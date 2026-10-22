"""Persistence and API tests for job items (#426)."""

import os
import tempfile

import pytest
import pytest_asyncio

from api.models.item import ItemUpdate, Origin, ReviewState
from api.models.job import JobCreate
from api.services.database import (
    close_db,
    create_job,
    delete_job,
    delete_job_items,
    get_job_item,
    get_job_items,
    init_db,
    update_job_item,
    upsert_job_items,
)
from api.services.items import (
    ITEM_SPECS,
    LARGE_VALUE_CHARS,
    SPECS_BY_KEY,
    SST_FIELD_IDS,
    Candidate,
    Proposal,
    build_items,
)
from api.services.legacy_item_adapter import extract_legacy_proposals


@pytest_asyncio.fixture
async def test_db():
    """Temporary database with the full schema."""
    import api.services.database as db_mod

    orig_engine = db_mod._engine
    orig_factory = db_mod._async_session_factory
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
    db_mod._engine = orig_engine
    db_mod._async_session_factory = orig_factory
    if orig_db_path is not None:
        os.environ["DATABASE_PATH"] = orig_db_path
    try:
        os.unlink(db_path)
    except Exception:
        pass


@pytest_asyncio.fixture
async def job(test_db):
    """A job to hang items off."""
    return await create_job(
        JobCreate(project_name="6POL0111", transcript_file="6POL0111.txt"),
    )


SEO_MD = """# SEO Report

### Title (Final Recommendation)

**Recommended:**
Polling the failed surplus deal | Inside Wisconsin Politics

### Short Description (90 chars max)

**Recommended:**
Zac Schultz breaks down the collapsed surplus negotiation.
"""


def rows_for(job_id, seo_md=SEO_MD, formatter_md="", sst_context=None):
    proposals = extract_legacy_proposals(seo_md=seo_md, formatter_md=formatter_md)
    return build_items(job_id=job_id, proposals=proposals, sst_context=sst_context)


@pytest.mark.asyncio
async def test_upsert_then_read_back(job):
    written = await upsert_job_items(job.id, rows_for(job.id))
    items = await get_job_items(job.id)

    assert written == len(ITEM_SPECS)
    assert len(items) == len(ITEM_SPECS)
    title = next(item for item in items if item.key == "title")
    assert title.proposed_value.startswith("Polling the failed surplus deal")
    assert title.origin == Origin.inferred
    assert title.review_state == ReviewState.proposed
    assert title.char_limit == 80


@pytest.mark.asyncio
async def test_items_are_returned_in_registry_order(job):
    await upsert_job_items(job.id, rows_for(job.id))

    keys = [item.key for item in await get_job_items(job.id)]

    assert keys == [spec.key for spec in ITEM_SPECS]


@pytest.mark.asyncio
async def test_upsert_is_idempotent(job):
    rows = rows_for(job.id)

    await upsert_job_items(job.id, rows)
    await upsert_job_items(job.id, rows)

    assert len(await get_job_items(job.id)) == len(ITEM_SPECS)


@pytest.mark.asyncio
async def test_reextraction_preserves_the_editors_columns(job):
    """A phase retry must not undo a verdict or discard a hand edit."""
    await upsert_job_items(job.id, rows_for(job.id))
    await update_job_item(
        job.id,
        "title",
        ItemUpdate(review_state=ReviewState.accepted, edited_value="The editor's title"),
    )

    await upsert_job_items(job.id, rows_for(job.id, seo_md=SEO_MD.replace("Polling", "Revised")))

    title = await get_job_item(job.id, "title")
    assert title.review_state == ReviewState.accepted
    assert title.edited_value == "The editor's title"
    assert title.proposed_value.startswith("Revised")


@pytest.mark.asyncio
async def test_editor_columns_survive_rows_that_carry_them(job):
    """The guarantee is enforced by the upsert, not by callers happening to
    omit the columns (pr-watch Low finding on #348)."""
    await upsert_job_items(job.id, rows_for(job.id))
    await update_job_item(
        job.id,
        "title",
        ItemUpdate(review_state=ReviewState.dismissed, edited_value="Kept"),
    )

    carrying = rows_for(job.id)
    for row in carrying:
        row["review_state"] = "proposed"
        row["edited_value"] = None
    await upsert_job_items(job.id, carrying)

    title = await get_job_item(job.id, "title")
    assert title.review_state == ReviewState.dismissed
    assert title.edited_value == "Kept"


@pytest.mark.asyncio
async def test_hand_edit_never_changes_origin(job):
    await upsert_job_items(job.id, rows_for(job.id))

    item = await update_job_item(job.id, "title", ItemUpdate(edited_value="A human wrote this"))

    assert item.edited_value == "A human wrote this"
    assert item.proposed_value.startswith("Polling")
    assert item.origin == Origin.inferred
    assert item.value == "A human wrote this"


@pytest.mark.asyncio
async def test_null_clears_an_edit_and_omission_keeps_it(job):
    await upsert_job_items(job.id, rows_for(job.id))
    await update_job_item(job.id, "title", ItemUpdate(edited_value="Edit"))

    kept = await update_job_item(job.id, "title", ItemUpdate(review_state=ReviewState.accepted))
    assert kept.edited_value == "Edit"

    cleared = await update_job_item(job.id, "title", ItemUpdate(edited_value=None))
    assert cleared.edited_value is None
    assert cleared.value == cleared.proposed_value


@pytest.mark.asyncio
async def test_review_state_can_be_reopened(job):
    await upsert_job_items(job.id, rows_for(job.id))
    await update_job_item(job.id, "title", ItemUpdate(review_state=ReviewState.dismissed))

    item = await update_job_item(job.id, "title", ItemUpdate(review_state=ReviewState.proposed))

    assert item.review_state == ReviewState.proposed


@pytest.mark.asyncio
async def test_unavailable_items_take_a_hand_edit(job):
    """Copy generation off still leaves the editor somewhere to write."""
    await upsert_job_items(job.id, rows_for(job.id))

    item = await update_job_item(job.id, "keywords", ItemUpdate(edited_value="politics, budget"))

    assert item.generation_state.value == "unavailable"
    assert item.value == "politics, budget"


@pytest.mark.asyncio
async def test_large_values_are_omitted_unless_requested(job):
    body = "x" * (LARGE_VALUE_CHARS + 1)
    await upsert_job_items(job.id, rows_for(job.id, formatter_md=body))

    omitted = await get_job_items(job.id, include_values=False)
    hydrated = await get_job_items(job.id, include_values=True)

    transcript_omitted = next(i for i in omitted if i.key == "formatted_transcript")
    transcript_full = next(i for i in hydrated if i.key == "formatted_transcript")
    assert transcript_omitted.proposed_value is None
    assert transcript_full.proposed_value == body
    # Small values survive the omission pass.
    assert next(i for i in omitted if i.key == "title").proposed_value is not None


@pytest.mark.asyncio
async def test_basis_round_trips(job):
    context = {field_id: None for field_id in SST_FIELD_IDS}
    context[SPECS_BY_KEY["short_description"].sst_field_id] = "Existing copy."
    context["record_id"] = "recTEST"

    await upsert_job_items(job.id, rows_for(job.id, sst_context=context))

    item = await get_job_item(job.id, "short_description")
    assert item.basis_value == "Existing copy."
    assert item.is_suggestion()
    assert item.basis_fetched_at is not None


@pytest.mark.asyncio
async def test_affirmation_round_trips_and_its_value_is_the_basis(job):
    context = {field_id: None for field_id in SST_FIELD_IDS}
    context[SPECS_BY_KEY["short_description"].sst_field_id] = "Existing copy."
    context["record_id"] = "recTEST"
    proposals = {"short_description": Proposal(value=None, origin=Origin.inferred, generator="g", affirm=True)}

    await upsert_job_items(job.id, build_items(job_id=job.id, proposals=proposals, sst_context=context))

    item = await get_job_item(job.id, "short_description")
    assert item.is_affirmation()
    assert item.proposed_value is None
    assert item.value == "Existing copy."

    await update_job_item(job.id, "short_description", ItemUpdate(edited_value="Hand edit."))
    item = await get_job_item(job.id, "short_description")
    assert item.value == "Hand edit."


@pytest.mark.asyncio
async def test_alternatives_and_flag_provenance_round_trip(job):
    proposals = {
        "title": Proposal(
            value="z" * 100,
            origin=Origin.inferred,
            generator="g",
            alternatives=(Candidate("Shorter title", angle="place"),),
        )
    }

    await upsert_job_items(job.id, build_items(job_id=job.id, proposals=proposals))

    item = await get_job_item(job.id, "title")
    assert [(alt.value, alt.angle, alt.length) for alt in item.alternatives] == [("Shorter title", "place", 13)]
    assert [(flag.origin.value, flag.target.value) for flag in item.flags] == [("code", "proposal")]
    assert item.model_dump()["alternatives"][0]["length"] == 13


@pytest.mark.asyncio
async def test_flags_written_before_provenance_still_read(job):
    """Rows without origin/target default to a code flag on the proposal."""
    rows = rows_for(job.id)
    for row in rows:
        if row["key"] == "title":
            row["flags"] = [{"check": "char_limit", "rule_id": "r", "severity": "error", "message": "m"}]
    await upsert_job_items(job.id, rows)

    flag = (await get_job_item(job.id, "title")).flags[0]
    assert (flag.origin.value, flag.target.value) == ("code", "proposal")


@pytest.mark.asyncio
async def test_a_malformed_stored_entry_drops_alone(job):
    good = {"check": "char_limit", "rule_id": "char_limit.max", "severity": "error", "message": "m"}
    rows = rows_for(job.id)
    for row in rows:
        if row["key"] == "title":
            row["flags"] = [{**good, "target": "both"}, good, "not a flag"]
            row["alternatives"] = [
                {"value": "Kept", "angle": "place", "flags": [{"check": "x"}, good]},
                {"angle": "no value"},
            ]
    await upsert_job_items(job.id, rows)

    item = await get_job_item(job.id, "title")
    assert [flag.rule_id for flag in item.flags] == ["char_limit.max"]
    assert [(alt.value, len(alt.flags)) for alt in item.alternatives] == [("Kept", 1)]


@pytest.mark.asyncio
async def test_flags_on_the_basis_do_not_count_as_errors(job):
    rows = rows_for(job.id)
    for row in rows:
        if row["key"] == "title":
            row["flags"] = [
                {"check": "char_limit", "rule_id": "r", "severity": "error", "message": "m", "target": "basis"}
            ]
    await upsert_job_items(job.id, rows)

    assert not (await get_job_item(job.id, "title")).has_errors()


@pytest.mark.asyncio
async def test_update_unknown_item_returns_none(job):
    assert await update_job_item(job.id, "nope", ItemUpdate(review_state=ReviewState.accepted)) is None


@pytest.mark.asyncio
async def test_delete_job_items(job):
    await upsert_job_items(job.id, rows_for(job.id))

    removed = await delete_job_items(job.id)

    assert removed == len(ITEM_SPECS)
    assert await get_job_items(job.id) == []


@pytest.mark.asyncio
async def test_items_are_scoped_to_their_job(test_db):
    job_a = await create_job(JobCreate(project_name="a", transcript_file="a.txt"))
    job_b = await create_job(JobCreate(project_name="b", transcript_file="b.txt"))

    await upsert_job_items(job_a.id, rows_for(job_a.id))

    assert len(await get_job_items(job_a.id)) == len(ITEM_SPECS)
    assert await get_job_items(job_b.id) == []

    await delete_job(job_b.id)


@pytest.mark.asyncio
async def test_item_pushes_table_exists_and_starts_empty(job):
    """The push record exists in the schema; nothing writes it yet (#428)."""
    from sqlalchemy import func, select

    from api.services.database import get_session, item_pushes_table

    await upsert_job_items(job.id, rows_for(job.id))

    async with get_session() as session:
        count = (await session.execute(select(func.count()).select_from(item_pushes_table))).scalar()
    assert count == 0


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoints_round_trip(job):
    """GET the list, GET one, PATCH an edit and a verdict — over real HTTP."""
    from fastapi.testclient import TestClient

    from api.main import app

    await upsert_job_items(job.id, rows_for(job.id))

    with TestClient(app) as client:
        listed = client.get(f"/api/jobs/{job.id}/items")
        assert listed.status_code == 200
        payload = listed.json()
        assert payload["total"] == len(ITEM_SPECS)
        assert payload["job_id"] == job.id
        assert payload["values_included"] is False
        assert payload["summary"]["by_generation_state"]["ready"] == 2  # title, short
        # The fixture has no long description section: a failure, not an absence.
        assert payload["summary"]["by_generation_state"]["failed"] == 1
        # Nothing was fetched, so every item is honestly 'unknown'.
        assert all(item["current_state"] == "unknown" for item in payload["items"])

        single = client.get(f"/api/jobs/{job.id}/items/title")
        assert single.status_code == 200
        assert single.json()["proposed_value"].startswith("Polling the failed surplus deal")

        patched = client.patch(
            f"/api/jobs/{job.id}/items/title",
            json={"edited_value": "Hand-written", "review_state": "accepted"},
        )
        assert patched.status_code == 200
        assert patched.json()["review_state"] == "accepted"
        assert patched.json()["edited_value"] == "Hand-written"
        assert patched.json()["origin"] == "inferred"

        accepted = client.get(f"/api/jobs/{job.id}/items", params={"review_state": "accepted"})
        assert [item["key"] for item in accepted.json()["items"]] == ["title"]
        assert accepted.json()["summary"]["edited"] == 1

        deterministic = client.get(f"/api/jobs/{job.id}/items", params={"origin": "deterministic"})
        assert {item["key"] for item in deterministic.json()["items"]} == {
            spec.key for spec in ITEM_SPECS if spec.origin == Origin.deterministic
        }

        ready = client.get(f"/api/jobs/{job.id}/items", params={"generation_state": "ready"})
        assert {item["key"] for item in ready.json()["items"]} == {"title", "short_description"}


@pytest.mark.asyncio
async def test_patch_rejects_system_fields(job):
    """Origin, basis, generation state and provenance are not the editor's."""
    from fastapi.testclient import TestClient

    from api.main import app

    await upsert_job_items(job.id, rows_for(job.id))

    with TestClient(app) as client:
        for body in (
            {"origin": "deterministic"},
            {"proposed_value": "sneaky"},
            {"basis_value": "x"},
            {"generation_state": "ready"},
            {"edited_value": "ok", "model": "x"},
        ):
            response = client.patch(f"/api/jobs/{job.id}/items/title", json=body)
            assert response.status_code == 422, body

        title = client.get(f"/api/jobs/{job.id}/items/title").json()
        assert title["origin"] == "inferred"
        assert title["edited_value"] is None


@pytest.mark.asyncio
async def test_endpoints_404_cleanly(job):
    from fastapi.testclient import TestClient

    from api.main import app

    with TestClient(app) as client:
        assert client.get("/api/jobs/999999/items").status_code == 404
        assert client.get(f"/api/jobs/{job.id}/items/nope").status_code == 404
        assert client.patch(f"/api/jobs/{job.id}/items/nope", json={"review_state": "accepted"}).status_code == 404
