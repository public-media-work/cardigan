"""QA-failure escalation + shared pause-and-suggest terminal handling (Spec B)."""

from __future__ import annotations

from datetime import datetime, timezone

from api.models.job import JobStatus, JobUpdate
from api.services import model_roster
from api.services.database import update_job

FAMILY_ORDER = ["haiku", "sonnet", "opus"]


def parse_model_family(model_slug: str | None) -> str | None:
    """Return 'haiku' | 'sonnet' | 'opus' parsed from a model slug, else None.

    Robust to OpenRouter's mixed word order (claude-4.6-sonnet vs claude-sonnet-4.6).
    """
    if not model_slug:
        return None
    s = model_slug.lower()
    for family in FAMILY_ORDER:
        if family in s:
            return family
    return None


def bump_family(family: str | None) -> str | None:
    """Return the next-stronger family, or None if already opus / unknown."""
    if family not in FAMILY_ORDER:
        return None
    idx = FAMILY_ORDER.index(family)
    return FAMILY_ORDER[idx + 1] if idx + 1 < len(FAMILY_ORDER) else None


async def pause_and_suggest(job_id: int, *, trigger: str, message: str, mark_escalated: bool = False) -> None:
    """Terminal handler shared by all failure triggers (QA-fail, credit, truncation).

    Leaves the job visibly NOT completed: status=paused with a structured,
    actionable error_message. Optionally stamps the escalate-once marker.
    """
    update = JobUpdate(
        status=JobStatus.paused,
        error_message=f"[{trigger}] {message}",
    )
    if mark_escalated:
        update.auto_escalated_at = datetime.now(timezone.utc)
    await update_job(job_id, update)


def select_escalation_phases(validation_result: dict, phase_order: list) -> list:
    """Earliest validator-flagged phase + every downstream phase in run order."""
    results = (validation_result or {}).get("phase_results", {})
    flagged = {name for name, r in results.items() if r.get("status") == "fail" or r.get("flags")}
    for i, name in enumerate(phase_order):
        if name in flagged:
            return phase_order[i:]
    return []


async def resolve_escalated_model(current_model: str | None, exclude_variants: list) -> str | None:
    """Bump current_model's family one step and resolve the newest catalog model
    in that family. None if already opus / unknown family / catalog unavailable.
    """
    target_family = bump_family(parse_model_family(current_model))
    if target_family is None:
        return None
    return await model_roster.newest_in_family(target_family, exclude_variants)


# Flag-text substrings (case-insensitive) that denote a failure a stronger
# model cannot fix — editorial review notes or missing input data.
NONFIXABLE_FLAG_PATTERNS = [
    # NOTE: review-notes / needs_review are deliberately ABSENT here (QA-1).
    # Substring-matching the validator's prose could not distinguish "review
    # notes are present when they should not be" from "the review-notes block
    # is missing" -- both contain "review note", so an ordinary, model-fixable
    # formatter miss was routed to a human with a message about media_id and
    # proper nouns. Since a single non-fixable flag suppresses escalation for
    # every fixable flag beside it, one misread sentence stalled the whole job.
    # The genuine case is detected on the ARTIFACT instead, via
    # FORMATTER_CONTRACT_MARKERS below: if the formatter really did leave review
    # notes or a needs_review status, they are in its output and we can see them.
    "media id",
    "media_id",
    # Caption-quality / verification-reminder flags observed on Here & Now web
    # clips (live jobs 15-19): a stronger model cannot supply missing source
    # data, external verification, or unavailable tooling.
    "unresolved",
    "not identified",
    "unidentified",
    "cannot be determined",
    "could not be determined",
    "unverified",
    "unconfirmed",
    "semrush",
    "excerpt",
    # Deterministic lint/style-engine flags explicitly marked non-model-fixable
    # (RuleViolation.to_flag_text()'s "[style-nonfixable:{rule_id}]" prefix,
    # e.g. lint.output_missing, lint.formatter.review_notes_in_body). A
    # stronger validator model cannot retroactively fix a phase's own output
    # -- that requires re-running the phase, which the escalation loop already
    # does for model-fixable flags; this pattern routes the truly mechanical,
    # non-model-fixable ones straight to a human-review pause instead of
    # (task 2b). Substring-matched against the lowercased flag text, so this
    # is NOT a substring of the model-fixable "[style:...]" prefix.
    "style-nonfixable",
]

# Markers the formatter writes into its OWN output when it surfaces an
# unresolved uncertainty. Their presence means the failure is a contract /
# editorial signal, not a model-quality defect. Matched case-insensitively.
FORMATTER_CONTRACT_MARKERS = [
    "<!-- review notes",
    "status:** needs_review",
    "status: needs_review",
]


def classify_qa_failure(validation_result: dict | None, context: dict | None = None) -> dict:
    """Split a failing validation_result's flags into model-fixable vs not.

    A flag is non-fixable when its text matches NONFIXABLE_FLAG_PATTERNS, or
    when the corresponding ``context["{phase}_output"]`` carries a formatter
    contract marker. Escalation is skipped only when EVERY failing flag is
    non-fixable. Empty/unknown input fails safe -> escalate=True.
    """
    context = context or {}
    results = (validation_result or {}).get("phase_results", {})
    fixable: list[str] = []
    nonfixable: list[str] = []

    for phase_name, r in results.items():
        flags = r.get("flags") or []
        if r.get("status") != "fail" and not flags:
            continue
        output = (context.get(f"{phase_name}_output") or "").lower()
        artifact_nonfixable = any(m in output for m in FORMATTER_CONTRACT_MARKERS)
        if not flags:
            # Phase failed with no flag text — only treat as non-fixable if the
            # artifact itself shows a contract marker; otherwise escalate.
            (nonfixable if artifact_nonfixable else fixable).append(f"{phase_name}: output failed")
            continue
        for flag in flags:
            ftext = (flag or "").lower()
            is_nonfixable = artifact_nonfixable or any(p in ftext for p in NONFIXABLE_FLAG_PATTERNS)
            (nonfixable if is_nonfixable else fixable).append(flag)

    # Escalation can only yield a PASS if EVERY flag clears, so a single
    # non-fixable flag makes escalation futile regardless of how many fixable
    # flags sit beside it -> skip whenever ANY non-fixable flag is present.
    # (Live evidence: jobs 15-19 each escalated to Opus and still failed.)
    # Empty/all-fixable -> escalate (fail safe / give the stronger model a shot).
    escalate = not nonfixable
    return {"escalate": escalate, "fixable": fixable, "nonfixable": nonfixable}


def nonfixable_review_message(nonfixable: list[str]) -> str:
    """Build the honest pause message naming the human-review items."""
    items = "; ".join(nonfixable) if nonfixable else "items the formatter could not verify"
    return (
        "Paused for human review — the formatter flagged items it can't verify "
        f"and a stronger model won't resolve: {items}. "
        "Verify media_id + proper-noun spelling, then resume."
    )
