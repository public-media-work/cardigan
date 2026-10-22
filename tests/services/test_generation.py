"""The V5 generation engine (#427): settings, the contract, and the runner.

The runner tests drive a real (temporary) database with a fake LLM and a
stubbed SST fetch — no network.
"""

import json
import os
import tempfile
from types import SimpleNamespace

import pytest
import pytest_asyncio

from api.models.item import REVIEW_ONLY, CopyMode, Origin
from api.models.job import JobCreate
from api.services.database import (
    close_db,
    create_job,
    edit_bundle_part,
    get_bundle_part,
    get_bundle_parts,
    get_job_item,
    get_job_items,
    init_db,
)
from api.services.generation import settings as gen_settings
from api.services.generation import slices as slices_mod
from api.services.generation.contract import (
    BundleView,
    Caps,
    GenerationFailure,
    GeneratorSpec,
    ResolvedSettings,
    UnitContext,
    parse_json_object,
)
from api.services.generation.runner import mark_stale, rebuild_slice, regenerate_item, run_job
from api.services.items import (
    COPY_REVIEW_NO_BASIS,
    SPECS_BY_KEY,
    SST_PREMIERE_FIELD_ID,
    Candidate,
    Proposal,
    build_items,
    join_title,
    unsupported_terms,
)

TITLE_ID = SPECS_BY_KEY["title"].sst_field_id
RESOLVED_MODEL = "anthropic/claude-sonnet-4.6-20260101"

SRT = """1
00:00:01,000 --> 00:00:04,000
Justin Howland carves bears out of logs.

2
00:00:05,000 --> 00:00:09,000
His shop, Grizz Works, sits outside Wausau.

3
00:00:40,000 --> 00:00:44,000
He started with a chainsaw and a dare.
"""

OUTLINE = {
    "summary": "Chainsaw artist Justin Howland carves wildlife at Grizz Works near Wausau.",
    "themes": ["chainsaw carving", "small business"],
    "segments": [{"start": "0:01", "summary": "Howland at work."}],
}


def title_reply(**overrides):
    reply = {
        "verdict": "revise",
        "proposal": "Justin Howland carves towering wildlife from logs",
        "rationale": "Names the artist and the craft.",
        "flags": [{"check": "missing_entity", "severity": "info", "target": "basis", "message": "No artist name."}],
        "alternatives": [
            {"value": "The chainsaw artist of Grizz Works", "angle": "place-driven"},
            {"value": "Justin Howland carves towering wildlife from logs", "angle": "repeat"},
            {"value": "From a dare to a chainsaw studio", "angle": "human-interest"},
        ],
    }
    reply.update(overrides)
    return json.dumps(reply)


class FakeLLM:
    """Answers by phase; records every call. ``replies`` values may be
    exceptions to raise."""

    def __init__(self, replies=None, config=None):
        self.replies = {"slice:outline": json.dumps(OUTLINE), "generator:title": title_reply()}
        self.replies.update(replies or {})
        self.config = config or {}
        self.calls = []

    async def chat(self, messages, backend=None, model=None, job_id=None, phase=None, **kwargs):
        self.calls.append({"phase": phase, "model": model, "kwargs": kwargs, "messages": messages})
        reply = self.replies[phase]
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(content=reply, model=RESOLVED_MODEL, input_tokens=1000, output_tokens=200, cost=0.004)

    def phases(self):
        return [call["phase"] for call in self.calls]


def sst_record(title="Grizz Works | Wisconsin Life", premiere="2026-05-28T22:00:00.000Z"):
    fields = {TITLE_ID: title, SST_PREMIERE_FIELD_ID: premiere, "fldO6YFVecVjLtMTy": "Justin Howland, Owner"}
    return {"id": "recGRIZZ", "fields": fields}


@pytest.fixture
def stub_sst(monkeypatch):
    """Replace the AirTable fetch with a canned record (or none)."""
    # The linked Project's name carries the season, as it does in the live base.
    state = {"record": sst_record(), "program": "Wisconsin Life | Season 13", "reason": None}

    async def fake_fetch(job, *, pin=True):
        return state["record"], state["program"], state["reason"]

    monkeypatch.setattr(slices_mod, "fetch_sst_record", fake_fetch)
    return state


@pytest_asyncio.fixture
async def test_db():
    import api.services.database as db_mod

    orig_engine, orig_factory = db_mod._engine, db_mod._async_session_factory
    orig_db_path = os.environ.get("DATABASE_PATH")
    db_mod._engine = None
    db_mod._async_session_factory = None

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.environ["DATABASE_PATH"] = db_path
    await init_db()
    from api.services.database import _engine, metadata

    async with _engine.begin() as conn:
        await conn.run_sync(metadata.create_all)

    yield db_path

    await close_db()
    db_mod._engine, db_mod._async_session_factory = orig_engine, orig_factory
    if orig_db_path is not None:
        os.environ["DATABASE_PATH"] = orig_db_path
    try:
        os.unlink(db_path)
    except Exception:
        pass


@pytest_asyncio.fixture
async def job(test_db):
    return await create_job(JobCreate(project_name="2WLIGrizzWorksSM", transcript_file="2WLIGrizzWorksSM.srt"))


INPUTS = {"srt_text": SRT, "transcript_text": SRT}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class TestSettings:
    def test_copy_mode_reads_the_old_boolean(self):
        assert CopyMode.parse(True) == CopyMode.suggest
        assert CopyMode.parse(False) == CopyMode.off
        assert CopyMode.parse("review") == CopyMode.review
        assert CopyMode.parse("nonsense") == CopyMode.suggest

    def test_engine_defaults_to_v4(self):
        assert gen_settings.engine({}) == "v4"
        assert gen_settings.engine({"routing": {"generation": {"engine": "items"}}}) == "items"
        assert gen_settings.engine({"routing": {"generation": {"engine": "v5?"}}}) == "v4"

    def test_job_override_wins_over_the_instance(self):
        config = {"routing": {"generation": {"copy_generation": "suggest"}}}
        assert gen_settings.job_copy_mode(SimpleNamespace(copy_generation="review"), config) == CopyMode.review
        assert gen_settings.job_copy_mode(SimpleNamespace(copy_generation=None), config) == CopyMode.suggest

    def test_instance_config_overrides_code_defaults(self):
        spec = GeneratorSpec(
            item="title", origin=Origin.inferred, run=None, model="m-default", caps=Caps(max_cost_usd=0.1)
        )
        config = {"routing": {"generation": {"generators": {"title": {"model": "m-instance", "enabled": False}}}}}

        resolved = gen_settings.resolve(spec, config)

        assert resolved.model == "m-instance"
        assert resolved.caps.max_cost_usd == 0.1
        assert resolved.enabled is False

    def test_a_deterministic_unit_never_gets_a_model(self):
        spec = GeneratorSpec(item="protrack_date", origin=Origin.deterministic, run=None)
        config = {"routing": {"generation": {"generators": {"protrack_date": {"model": "m"}}}}}
        assert gen_settings.resolve(spec, config).model is None


# ---------------------------------------------------------------------------
# Contract pieces
# ---------------------------------------------------------------------------


class TestContract:
    def test_reads_are_enforced(self):
        view = BundleView("title", {}, ("job",))
        assert view.get("job") is None
        with pytest.raises(GenerationFailure) as err:
            view.get("captions")
        assert err.value.reason == "contract:reads"

    def test_parse_json_tolerates_a_fence(self):
        assert parse_json_object('Here:\n```json\n{"a": 1}\n```') == {"a": 1}
        assert parse_json_object('prose {"a": 2} prose') == {"a": 2}
        with pytest.raises(GenerationFailure) as err:
            parse_json_object("no json at all")
        assert err.value.reason == "malformed_output"

    @pytest.mark.asyncio
    async def test_cost_cap_is_checked_before_the_call(self):
        llm = FakeLLM()
        ctx = UnitContext(
            unit="generator:title",
            job=SimpleNamespace(id=1),
            copy_mode=CopyMode.suggest,
            settings=ResolvedSettings(
                model="anthropic/claude-opus-4.6", backend=None, caps=Caps(max_cost_usd=0.0001, max_output_tokens=4000)
            ),
            llm=llm,
        )
        with pytest.raises(GenerationFailure) as err:
            await ctx.complete("system " * 1000, "user")
        assert err.value.reason == "cap:cost"
        assert llm.calls == []

    def test_join_title_follows_the_house_convention(self):
        assert join_title("Grizz Works", "Wisconsin Life") == "Wisconsin Life | Grizz Works"
        assert join_title("Track records", "Inside Wisconsin Politics") == "Track records | Inside Wisconsin Politics"
        assert join_title("Wisconsin Life | Grizz Works", "Wisconsin Life") == "Wisconsin Life | Grizz Works"
        assert join_title("Untitled", None) == "Untitled"

    def test_the_series_drops_the_season(self):
        assert slices_mod.series_name("Wisconsin Life | Season 13") == "Wisconsin Life"
        assert slices_mod.series_name("Here & Now") == "Here & Now"
        assert slices_mod.series_name(None) is None

    def test_unsupported_terms_catch_a_misspelled_name(self):
        known = "Justin Howland carves bears. Howland's shop is world-class."
        assert unsupported_terms("Jusstin Howland's world-class shop", known) == ["Jusstin"]
        assert unsupported_terms("World-class carver Justin Howland", known) == []

    def test_captions_become_timed_blocks(self):
        captions = {
            "cues": [
                {"start_ms": 1000, "end_ms": 4000, "text": "One."},
                {"start_ms": 31000, "end_ms": 34000, "text": "Two."},
                {"start_ms": 40000, "end_ms": 44000, "text": "Three."},
            ]
        }
        assert slices_mod.captions_as_blocks(captions) == "[0:01] One. Two.\n[0:40] Three."


# ---------------------------------------------------------------------------
# Copy modes in assembly
# ---------------------------------------------------------------------------


class TestCopyModes:
    def rows(self, mode, basis="Kept | Program", **proposal):
        proposal = Proposal(origin=Origin.inferred, generator="g", **{"value": "New | Program", **proposal})
        context = {TITLE_ID: basis, "record_id": "rec"} if basis else None
        return {
            row["key"]: row
            for row in build_items(job_id=1, proposals={"title": proposal}, sst_context=context, copy_generation=mode)
        }["title"]

    def test_review_drops_text_and_keeps_flags(self):
        flag = {"check": "keyword", "rule_id": "model.keyword", "severity": "info", "message": "m"}
        row = self.rows("review", alternatives=(Candidate("Alt"),), flags=(flag,))

        assert row["generation_state"] == "ready"
        assert row["generation_reason"] == REVIEW_ONLY
        assert row["proposed_value"] is None
        assert row["alternatives"] == []
        assert [(f["check"], f["target"]) for f in row["flags"]] == [("keyword", "basis")]

    def test_review_with_no_basis_is_unavailable(self):
        row = self.rows("review", basis=None)
        assert (row["generation_state"], row["generation_reason"]) == ("unavailable", COPY_REVIEW_NO_BASIS)

    def test_suggest_keeps_the_proposal(self):
        assert self.rows("suggest")["proposed_value"] == "New | Program"

    def test_off_is_unavailable(self):
        assert self.rows("off")["generation_reason"] == "copy_generation_off"


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_job_runs_end_to_end(job, stub_sst):
    llm = FakeLLM()

    summary = await run_job(job.id, llm=llm, llm_config={}, inputs=INPUTS)

    assert summary["slices"] == {name: "ready" for name in ("sst", "captions", "job", "house_rules", "outline")}
    assert llm.phases() == ["slice:outline", "generator:title"]

    title = await get_job_item(job.id, "title")
    assert title.generation_state.value == "ready"
    assert title.proposed_value == "Wisconsin Life | Justin Howland carves towering wildlife from logs"
    assert title.basis_value == "Grizz Works | Wisconsin Life"
    assert title.model == RESOLVED_MODEL
    assert (title.input_tokens, title.cost_usd) == (1000, 0.004)
    # The repeat of the proposal is dropped in code; two alternatives survive.
    assert [alt.angle for alt in title.alternatives] == ["place-driven", "human-interest"]
    assert title.alternatives[0].value == "Wisconsin Life | The chainsaw artist of Grizz Works"
    assert [(f.origin.value, f.target.value) for f in title.flags] == [("model", "basis")]
    assert set(title.input_revisions["slices"]) == {"job", "sst", "outline", "house_rules"}

    dates = [await get_job_item(job.id, key) for key in ("media_manager_date", "protrack_date")]
    assert {d.proposed_value for d in dates} == {"2026-05-28"}
    assert {d.origin.value for d in dates} == {"deterministic"}

    keywords = await get_job_item(job.id, "keywords")
    assert keywords.generation_state.value == "unavailable"

    job_slice = await get_bundle_part(job.id, "job")
    assert job_slice.content["program"] == "Wisconsin Life"
    assert job_slice.content["title_convention"]["program_position"] == "prefix"
    assert job_slice.content["settings"]["generators"]["title"]["model"] == "anthropic/claude-sonnet-4.6"

    outline = await get_bundle_part(job.id, "outline")
    assert outline.origin.value == "inferred"
    assert outline.model == RESOLVED_MODEL


@pytest.mark.asyncio
async def test_the_title_prompt_never_sees_the_captions(job, stub_sst):
    llm = FakeLLM()
    await run_job(job.id, llm=llm, llm_config={}, inputs=INPUTS)

    title_call = next(call for call in llm.calls if call["phase"] == "generator:title")
    user = title_call["messages"][1]["content"]
    assert "carves bears out of logs" not in user  # caption text
    assert "Grizz Works" in user  # the basis, without the program name
    assert "Wisconsin Life" in user  # as context only
    assert title_call["kwargs"]["max_tokens"] == 2000


@pytest.mark.asyncio
async def test_a_failed_slice_cascades_only_to_its_readers(job, stub_sst):
    llm = FakeLLM({"slice:outline": "not json"})

    summary = await run_job(job.id, llm=llm, llm_config={}, inputs=INPUTS)

    assert summary["slices"]["outline"] == "failed"
    title = await get_job_item(job.id, "title")
    assert (title.generation_state.value, title.generation_reason) == ("unavailable", "slice:outline")
    assert "generator:title" not in llm.phases()
    assert (await get_job_item(job.id, "protrack_date")).generation_state.value == "ready"


@pytest.mark.asyncio
async def test_a_failed_generator_is_its_item_state(job, stub_sst):
    from api.services.llm import OutputTruncatedError

    llm = FakeLLM({"generator:title": OutputTruncatedError("cut off")})

    await run_job(job.id, llm=llm, llm_config={}, inputs=INPUTS)

    title = await get_job_item(job.id, "title")
    assert (title.generation_state.value, title.generation_reason) == ("failed", "cap:output_tokens")
    assert (await get_job_item(job.id, "transcript_date")).generation_state.value == "ready"


@pytest.mark.asyncio
async def test_no_sst_record_means_a_first_draft(job, stub_sst):
    stub_sst.update(record=None, program=None, reason="no_sst_record")
    llm = FakeLLM()

    await run_job(job.id, llm=llm, llm_config={}, inputs=INPUTS)

    sst = await get_bundle_part(job.id, "sst")
    assert (sst.state.value, sst.reason, sst.content["fetched"]) == ("ready", "no_sst_record", False)
    title = await get_job_item(job.id, "title")
    assert title.basis_value is None
    assert title.proposed_value == "Justin Howland carves towering wildlife from logs"
    dates = await get_job_item(job.id, "protrack_date")
    assert dates.generation_reason == "no_sst_record"


@pytest.mark.asyncio
async def test_review_mode_keeps_the_basis(job, stub_sst):
    llm = FakeLLM(config={})
    config = {"routing": {"generation": {"copy_generation": "review"}}}

    await run_job(job.id, llm=llm, llm_config=config, inputs=INPUTS)

    title = await get_job_item(job.id, "title")
    assert title.generation_reason == REVIEW_ONLY
    assert title.proposed_value is None
    assert title.alternatives == []
    assert title.value == "Grizz Works | Wisconsin Life"
    review_call = next(call for call in llm.calls if call["phase"] == "generator:title")
    assert "## Mode\n\nreview" in review_call["messages"][1]["content"]


@pytest.mark.asyncio
async def test_copy_off_makes_no_model_call_for_copy(job, stub_sst):
    llm = FakeLLM()
    config = {"routing": {"generation": {"copy_generation": "off"}}}

    await run_job(job.id, llm=llm, llm_config=config, inputs=INPUTS)

    assert "generator:title" not in llm.phases()
    title = await get_job_item(job.id, "title")
    assert title.generation_reason == "copy_generation_off"
    assert title.basis_value == "Grizz Works | Wisconsin Life"


@pytest.mark.asyncio
async def test_a_disabled_generator_is_unavailable(job, stub_sst):
    llm = FakeLLM()
    config = {"routing": {"generation": {"generators": {"title": {"enabled": False}}}}}

    await run_job(job.id, llm=llm, llm_config=config, inputs=INPUTS)

    title = await get_job_item(job.id, "title")
    assert (title.generation_state.value, title.generation_reason) == ("unavailable", "generator_disabled")


@pytest.mark.asyncio
async def test_a_correction_makes_readers_stale_and_survives_a_rebuild(job, stub_sst):
    llm = FakeLLM()
    await run_job(job.id, llm=llm, llm_config={}, inputs=INPUTS)
    before = await get_bundle_part(job.id, "outline")

    corrected = {**OUTLINE, "summary": "Justin Howland, owner of Grizz Works, carves wildlife."}
    edited = await edit_bundle_part(job.id, "outline", corrected)
    assert edited.revision == before.revision + 1
    assert edited.effective == corrected

    items = mark_stale(await get_job_items(job.id), await get_bundle_parts(job.id))
    stale = {item.key for item in items if item.stale}
    assert stale == {"title"}  # the dates read only the sst slice

    rebuilt = await rebuild_slice(job.id, "outline", llm=llm, llm_config={})
    assert rebuilt.edited_content == corrected
    assert rebuilt.revision == edited.revision  # same effective content: no new revision

    item = await regenerate_item(job.id, "title", llm=llm, llm_config={})
    title_call = [call for call in llm.calls if call["phase"] == "generator:title"][-1]
    assert "owner of Grizz Works" in title_call["messages"][1]["content"]
    assert not mark_stale([item], await get_bundle_parts(job.id))[0].stale


@pytest.mark.asyncio
async def test_re_running_keeps_the_editors_verdict(job, stub_sst):
    from api.models.item import ItemUpdate, ReviewState
    from api.services.database import update_job_item

    llm = FakeLLM()
    await run_job(job.id, llm=llm, llm_config={}, inputs=INPUTS)
    await update_job_item(job.id, "title", ItemUpdate(edited_value="Mine", review_state=ReviewState.accepted))

    await regenerate_item(job.id, "title", llm=llm, llm_config={})

    title = await get_job_item(job.id, "title")
    assert (title.edited_value, title.review_state) == ("Mine", ReviewState.accepted)


@pytest.mark.asyncio
async def test_captions_cannot_be_rebuilt_without_the_file(job, stub_sst):
    await run_job(job.id, llm=FakeLLM(), llm_config={}, inputs=INPUTS)
    with pytest.raises(ValueError):
        await rebuild_slice(job.id, "captions", llm=FakeLLM(), llm_config={})


@pytest.mark.asyncio
async def test_plain_text_takes_the_degraded_path(job, stub_sst):
    await run_job(job.id, llm=FakeLLM(), llm_config={}, inputs={"transcript_text": "Just words here."})
    captions = await get_bundle_part(job.id, "captions")
    assert captions.content["timed"] is False
    assert captions.content["text"] == "Just words here."


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bundle_endpoints(job, stub_sst):
    from fastapi.testclient import TestClient

    from api.main import app

    await run_job(job.id, llm=FakeLLM(), llm_config={}, inputs=INPUTS)

    with TestClient(app) as client:
        bundle = client.get(f"/api/jobs/{job.id}/bundle").json()
        assert [part["slice"] for part in bundle["parts"]] == ["sst", "captions", "job", "house_rules", "outline"]

        patched = client.patch(f"/api/jobs/{job.id}/bundle/outline", json={"edited_content": {"summary": "S"}})
        assert patched.status_code == 200
        assert patched.json()["edited_content"] == {"summary": "S"}

        assert client.patch(f"/api/jobs/{job.id}/bundle/outline", json={"content": {}}).status_code == 422

        listed = client.get(f"/api/jobs/{job.id}/items").json()
        assert listed["summary"]["stale"] == 1
        assert client.get(f"/api/jobs/{job.id}/items/title").json()["stale"] is True

        assert client.post(f"/api/jobs/{job.id}/items/keywords/regenerate").status_code == 409
        assert client.post(f"/api/jobs/{job.id}/bundle/captions/rebuild").status_code == 409
        assert client.post(f"/api/jobs/{job.id}/bundle/nope/rebuild").status_code == 404


@pytest.mark.asyncio
async def test_a_misspelled_name_is_flagged_by_code(job, stub_sst):
    """2WLIGrizzWorksSM, 2026-10-22: the outline wrote 'Jusstin' and the title copied it."""
    outline = {**OUTLINE, "summary": "Jusstin Howland carves wildlife at Grizz Works."}
    llm = FakeLLM(
        {
            "slice:outline": json.dumps(outline),
            "generator:title": title_reply(proposal="Jusstin Howland carves wildlife", alternatives=[]),
        }
    )

    await run_job(job.id, llm=llm, llm_config={}, inputs=INPUTS)

    assert (await get_bundle_part(job.id, "outline")).content["unverified_terms"] == ["Jusstin"]
    title = await get_job_item(job.id, "title")
    grounding = [f for f in title.flags if f.check == "grounding"]
    assert [(f.origin.value, f.target.value, f.severity) for f in grounding] == [("code", "proposal", "warning")]
    assert "Jusstin" in grounding[0].message


@pytest.mark.asyncio
async def test_the_title_prompt_states_the_limits(job, stub_sst):
    llm = FakeLLM()
    await run_job(job.id, llm=llm, llm_config={}, inputs=INPUTS)
    user = next(call for call in llm.calls if call["phase"] == "generator:title")["messages"][1]["content"]
    assert "80 characters or fewer" in user
    assert "100 or fewer" in user


@pytest.mark.asyncio
async def test_a_typo_inside_a_source_is_a_spelling_conflict(job, stub_sst):
    """The live record's Lower Thirds read 'Jusstin Howland' while the captions said
    'Justin'. The typo is 'known', so only the conflict check can catch it."""
    record = sst_record()
    record["fields"]["fldO6YFVecVjLtMTy"] = "Jusstin Howland"
    record["fields"][SPECS_BY_KEY["short_description"].sst_field_id] = "Chainsaw artist Justin Howland carves."
    stub_sst["record"] = record
    outline = {**OUTLINE, "summary": "Jusstin Howland carves wildlife at Grizz Works."}
    llm = FakeLLM(
        {
            "slice:outline": json.dumps(outline),
            "generator:title": title_reply(proposal="Jusstin Howland carves wildlife", alternatives=[]),
        }
    )

    await run_job(job.id, llm=llm, llm_config={}, inputs=INPUTS)

    conflicts = (await get_bundle_part(job.id, "outline")).content["name_conflicts"]
    assert [(c["used"], c["preferred"]) for c in conflicts] == [("Jusstin", "Justin")]
    title = await get_job_item(job.id, "title")
    assert [f.rule_id for f in title.flags if f.check == "grounding"] == ["grounding.spelling_conflict"]
