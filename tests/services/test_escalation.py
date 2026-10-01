import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from api.models.job import JobCreate, JobStatus
from api.services import escalation
from api.services.database import create_job, get_job
from api.services.escalation import (
    bump_family,
    classify_qa_failure,
    nonfixable_review_message,
    parse_model_family,
    pause_and_suggest,
    select_escalation_phases,
)
from tests.api.test_database import test_db  # noqa: F401

PHASE_ORDER = ["analyst", "formatter", "seo", "validator", "timestamp"]


@pytest.mark.parametrize(
    "slug,expected",
    [
        ("anthropic/claude-4.5-haiku-20251001", "haiku"),
        ("anthropic/claude-4.6-sonnet-20260217", "sonnet"),
        ("anthropic/claude-sonnet-4.6", "sonnet"),  # word order varies
        ("anthropic/claude-opus-4-8", "opus"),
        ("openai/gpt-4o", None),
        (None, None),
        ("", None),
    ],
)
def test_parse_model_family(slug, expected):
    assert parse_model_family(slug) == expected


@pytest.mark.parametrize(
    "family,expected",
    [
        ("haiku", "sonnet"),
        ("sonnet", "opus"),
        ("opus", None),  # terminal
        (None, None),
        ("mystery", None),
    ],
)
def test_bump_family(family, expected):
    assert bump_family(family) == expected


@pytest.mark.asyncio
async def test_pause_and_suggest_sets_paused_and_marker(test_db):  # noqa: F811
    job = await create_job(JobCreate(project_name="p", project_path="/p", transcript_file="/t.txt"))
    await pause_and_suggest(job.id, trigger="qa_fail", message="QA failed — review or retry.", mark_escalated=True)
    refreshed = await get_job(job.id)
    assert refreshed.status == JobStatus.paused
    assert "QA failed" in refreshed.error_message
    assert refreshed.auto_escalated_at is not None


def test_selects_earliest_flagged_plus_downstream():
    vr = {
        "overall": "fail",
        "phase_results": {
            "analyst": {"status": "pass", "flags": []},
            "formatter": {"status": "fail", "flags": ["x"]},
            "seo": {"status": "pass", "flags": []},
        },
    }
    assert select_escalation_phases(vr, PHASE_ORDER) == ["formatter", "seo", "validator", "timestamp"]


def test_no_flags_returns_empty():
    vr = {"overall": "pass", "phase_results": {"formatter": {"status": "pass", "flags": []}}}
    assert select_escalation_phases(vr, PHASE_ORDER) == []


@pytest.mark.asyncio
async def test_resolve_escalated_model_bumps_and_resolves():
    with patch.object(
        escalation.model_roster, "newest_in_family", AsyncMock(return_value="anthropic/claude-sonnet-4-6")
    ) as m:
        got = await escalation.resolve_escalated_model("anthropic/claude-4.5-haiku-20251001", ["fast", "fable"])
    assert got == "anthropic/claude-sonnet-4-6"
    m.assert_awaited_once_with("sonnet", ["fast", "fable"])


@pytest.mark.asyncio
async def test_resolve_escalated_model_none_when_opus():
    assert await escalation.resolve_escalated_model("anthropic/claude-opus-4-8", ["fast"]) is None


def test_config_has_qa_escalation_and_sonnet_validator():
    cfg = json.loads((Path(__file__).resolve().parents[2] / "config" / "llm-config.json").read_text())
    qa = cfg["qa_escalation"]
    assert qa["on_validation_fail"] is True
    assert qa["max_auto_escalations"] == 1
    assert qa["skip_escalation_when_nonfixable"] is True
    assert qa["exclude_variants"] == ["fast", "fable"]
    # validator no longer on the cheapskate tier
    assert cfg["phase_backends"]["validator"] != "openrouter-cheapskate"


# ---------------------------------------------------------------------------
# classify_qa_failure / nonfixable_review_message (#276)
# ---------------------------------------------------------------------------


def _vr(formatter_flags=None, seo_flags=None):
    return {
        "overall": "fail",
        "phase_results": {
            "analyst": {"status": "pass", "flags": []},
            "formatter": {"status": "fail" if formatter_flags else "pass", "flags": formatter_flags or []},
            "seo": {"status": "fail" if seo_flags else "pass", "flags": seo_flags or []},
        },
    }


def test_classify_review_notes_only_skips():
    """Review notes genuinely left in the body skip escalation (detected on the artifact).

    Rebased from flag prose onto the artifact in QA-1: the output is what proves
    the notes are there. Matching the sentence could not tell this from a flag
    reporting the block MISSING, and misrouted the latter to a human.
    """
    ctx = {"formatter_output": "# Formatted Transcript\n<!-- REVIEW NOTES:\n- verify spelling\n-->\n"}
    out = classify_qa_failure(_vr(formatter_flags=["Review notes appear in transcript body"]), ctx)
    assert out["escalate"] is False
    assert out["nonfixable"] and not out["fixable"]


def test_classify_needs_review_text_skips():
    """A real needs_review status skips escalation — read from the artifact (QA-1)."""
    ctx = {"formatter_output": "# Formatted Transcript\n\n**Status:** needs_review\n"}
    out = classify_qa_failure(_vr(formatter_flags=["Status field 'needs_review' indicates incomplete processing"]), ctx)
    assert out["escalate"] is False


def test_classify_artifact_marker_skips_vague_flag():
    # Flag text is vague, but the formatter OUTPUT carries the contract marker.
    vr = _vr(formatter_flags=["something is off"])
    ctx = {"formatter_output": "# Formatted Transcript\n<!-- REVIEW NOTES:\n- verify spelling\n-->\n"}
    out = classify_qa_failure(vr, ctx)
    assert out["escalate"] is False


def test_classify_mixed_with_nonfixable_skips():
    # A single non-fixable flag makes escalation futile even when a model-fixable
    # flag sits beside it (the job still can't pass) -> skip escalation.
    # Live evidence: jobs 15-19 each escalated to Opus and still failed.
    ctx = {"formatter_output": "# Formatted Transcript\n<!-- REVIEW NOTES:\n- verify\n-->\n"}
    out = classify_qa_failure(
        _vr(formatter_flags=["Review notes appear in transcript body"], seo_flags=["title exceeds 60 characters"]),
        ctx,
    )
    assert out["escalate"] is False
    assert out["fixable"] and out["nonfixable"]


def test_classify_caption_quality_only_skips():
    # Caption-quality / verification flags with NO literal "review notes" line —
    # still non-fixable (no source data, external verification, missing tooling).
    out = classify_qa_failure(
        _vr(
            formatter_flags=["Speaker identity unresolved — no name in source transcript"],
            seo_flags=["Speaker's official title unverified; SEMRush validation not completed"],
        ),
        {},
    )
    assert out["escalate"] is False
    assert out["nonfixable"] and not out["fixable"]


def test_classify_truncation_only_escalates():
    out = classify_qa_failure(_vr(formatter_flags=["content ends abruptly mid-sentence (truncation)"]), {})
    assert out["escalate"] is True


def test_classify_empty_failsafe_escalates():
    assert classify_qa_failure({}, {})["escalate"] is True
    assert classify_qa_failure(None, None)["escalate"] is True


def test_nonfixable_review_message_includes_flags():
    msg = nonfixable_review_message(["Review notes appear in transcript body"])
    assert "human review" in msg.lower()
    assert "Review notes appear in transcript body" in msg


# ---------------------------------------------------------------------------
# classify_qa_failure -- "[style-nonfixable:...]" prefix (task 2b)
# ---------------------------------------------------------------------------


def test_classify_style_nonfixable_prefix_skips():
    out = classify_qa_failure(
        _vr(formatter_flags=["[style-nonfixable:lint.output_missing] formatter output is missing"]),
        {},
    )
    assert out["escalate"] is False
    assert out["nonfixable"] and not out["fixable"]


def test_classify_style_fixable_prefix_alone_escalates():
    # The model-fixable "[style:...]" prefix must NOT be caught by the new
    # "style-nonfixable" pattern -- "style-nonfixable" is not a substring of
    # "[style:lint.seo.title_over_limit] ...".
    out = classify_qa_failure(
        _vr(seo_flags=["[style:lint.seo.title_over_limit] title exceeds 80 characters"]),
        {},
    )
    assert out["escalate"] is True
    assert out["fixable"] and not out["nonfixable"]


# ─── QA-1: prose substring matching misroutes model-fixable flags ──────


def test_classify_flag_about_missing_review_notes_escalates():
    """A flag about the ABSENCE of a review-notes block is model-fixable (QA-1).

    Substring matching could not tell "review notes are present when they should
    not be" — a real contract signal, and one the ARTIFACT shows — from "the
    review-notes block is missing", which is ordinary output a re-run can fix.
    Both contain "review note", so both were routed to a human with a message
    about media_id and proper nouns. Live run 6POL0213 hit exactly this.
    """
    vr = _vr(formatter_flags=["Missing <!-- REVIEW NOTES --> block in formatter output"])
    clean_artifact = {"formatter_output": "# Formatted Transcript\n\n**Status:** ready_for_editing\n"}
    out = classify_qa_failure(vr, clean_artifact)
    assert out["escalate"] is True
    assert out["fixable"] and not out["nonfixable"]


def test_classify_still_skips_genuinely_unfixable_flags():
    """Narrowing the review-notes patterns must not loosen the others (QA-1 guard)."""
    for flag in ("Media ID could not be determined", "Speaker remains unidentified", "SEMrush data unavailable"):
        out = classify_qa_failure(_vr(formatter_flags=[flag]), {})
        assert out["escalate"] is False, f"{flag!r} must stay non-fixable"
