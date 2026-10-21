"""Shared review-notes-placement check.

Pure stdlib -- no dependencies beyond ``style_engine.types``. Detects a
review-note marker (an HTML "<!-- REVIEW..." comment, a literal
"NEEDS_REVIEW" token, or a "## Review Notes" heading) appearing AFTER the
formatter document's first horizontal rule -- i.e., inside the transcript
body instead of at the top where house style (``phases.formatter.
review_notes.placement == "top"``) requires it.

The contract's own ``**Status:**`` footer is exempt: it legitimately sits
after the final horizontal rule, and its ``needs_review`` value is a status,
not a stray review note.

This check has exactly one implementation, shared by two call sites that
would otherwise carry duplicate copies of the same regex/logic:
``lint.run_lint`` (a validator-time re-check over the phase-output context
bus) and ``post_stage.run_post_stage``'s formatter path (checked immediately
after normalization, on the freshly-normalized text). Both produce the same
``lint.formatter.review_notes_in_body`` rule_id -- it is the same detector,
just invoked at two different points in the pipeline.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from api.services.style_engine.types import RuleViolation

_HR_LINE_RE = re.compile(r"^-{3,}[ \t]*$", re.MULTILINE)
_REVIEW_NOTE_MARKER_RE = re.compile(r"<!--\s*review|NEEDS_REVIEW|^##\s*Review Notes", re.IGNORECASE | re.MULTILINE)

# The formatter contract (prompts/formatter.md) REQUIRES a closing
# ``**Status:** {ready_for_editing | needs_review}`` footer, and that footer
# necessarily sits after the document's final horizontal rule. Its
# ``needs_review`` value would otherwise match _REVIEW_NOTE_MARKER_RE's bare
# NEEDS_REVIEW alternative, so every transcript that correctly asks for human
# review would be flagged as having a review note loose in its body. Blank the
# footer line before scanning -- length-preserving, so match offsets in any
# future span-reporting stay true to the original text.
_STATUS_FOOTER_LINE_RE = re.compile(r"^\*\*Status:\*\*[^\n]*$", re.IGNORECASE | re.MULTILINE)


def check_review_notes_placement(raw_output: str, review_notes_cfg: Mapping | None, phase: str) -> list[RuleViolation]:
    """Detect a review-note marker after the first horizontal rule.

    ``review_notes_cfg`` is ``phases.formatter.review_notes``-shaped (keys:
    ``placement``, ``format``, ``tier``). A no-op (returns ``[]``) unless
    ``placement == "top"`` -- other placement policies aren't this check's
    concern. Detection only: never rewrites or moves the review note.
    """
    if (review_notes_cfg or {}).get("placement") != "top":
        return []

    hr_match = _HR_LINE_RE.search(raw_output)
    if not hr_match:
        return []

    after_first_rule = _STATUS_FOOTER_LINE_RE.sub(lambda m: " " * len(m.group(0)), raw_output[hr_match.end() :])
    marker_match = _REVIEW_NOTE_MARKER_RE.search(after_first_rule)
    if not marker_match:
        return []

    return [
        RuleViolation(
            rule_id="lint.formatter.review_notes_in_body",
            phase=phase,
            severity="error",
            message=(
                f'Review-note marker "{marker_match.group(0).strip()}" appears after the first '
                "horizontal rule -- review notes must sit at the top of the document"
            ),
            model_fixable=False,
        )
    ]
