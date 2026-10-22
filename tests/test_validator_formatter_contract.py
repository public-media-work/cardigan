"""Drift test: the validator's checklist vs. the formatter's output contract.

`prompts/formatter.md` REQUIRES two pieces of structure in every formatted
transcript: a `<!-- REVIEW NOTES: ... -->` HTML comment immediately after the
metadata header (when the formatter has unresolved items) and a
`**Status:** {ready_for_editing | needs_review}` footer.

`prompt_blocks."validator.checklist"` in config/house_style.yaml is what tells
the LLM validator what counts as a formatter failure. If that checklist
blanket-forbids "review notes / metadata in the transcript body" without
qualifying by PLACEMENT, it contradicts the formatter contract and the
validator fails every contract-compliant transcript that happens to carry a
review note.

That is not hypothetical: production jobs 31-36 (the 2MSY batch) all failed
validation on flag text near-verbatim from this checklist and from the JSON
example in prompts/validator.md, while job 23 -- six review notes at the top,
`Status: ready_for_editing` -- passed clean. The discriminator was the
`needs_review` footer, not the notes.

The deterministic linter already models this correctly
(`api/services/style_engine/review_notes.check_review_notes_placement` checks
placement; `lint._body_region` treats the `**Status:**` footer as structure),
so these tests hold the LLM-facing prose to the same contract the code keeps.
"""

from __future__ import annotations

import re
from pathlib import Path

from api.services.style_engine.rules import load_rules

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "house_style.yaml"
VALIDATOR_PROMPT_PATH = REPO_ROOT / "prompts" / "validator.md"
FORMATTER_PROMPT_PATH = REPO_ROOT / "prompts" / "formatter.md"


def _checklist(profile: str = "full") -> str:
    rules = load_rules(CONFIG_PATH)
    block = (rules.raw.get("prompt_blocks", {}) or {}).get("validator.checklist")
    assert block is not None, "prompt_blocks.'validator.checklist' is missing from house_style.yaml"
    text = block.get(profile)
    assert text, f"validator.checklist has no {profile!r} profile"
    return text


def _section(text: str, heading_re: str) -> str:
    """Return the body of the '### <heading>' section, up to the next heading."""
    m = re.search(rf"^###\s+{heading_re}\s*$(.*?)(?=^###\s|\Z)", text, re.M | re.S)
    assert m, f"validator.checklist is missing a '### {heading_re}' section"
    return m.group(1)


class TestFormatterContractIsNotContradicted:
    def test_formatter_prompt_still_requires_the_structure_under_test(self):
        """Guard the premise: if the formatter contract changes, these tests
        must be revisited rather than silently passing against a moved target.
        """
        formatter = FORMATTER_PROMPT_PATH.read_text()
        assert "<!-- REVIEW NOTES" in formatter
        assert "**Status:**" in formatter

    def test_review_notes_defect_is_qualified_by_placement(self):
        """A formatter bullet mentioning review notes must say WHERE they are a
        defect (inline / scattered / end), not forbid them outright -- the
        contract puts them at the top.
        """
        section = _section(_checklist(), "Formatter Phase")
        note_lines = [ln for ln in section.splitlines() if re.search(r"review note", ln, re.I)]
        assert note_lines, "Formatter Phase section says nothing about review notes"

        placement_words = re.compile(r"inline|scattered|top of|end of|placement|after the metadata", re.I)
        unqualified = [ln for ln in note_lines if not placement_words.search(ln)]
        assert not unqualified, (
            "validator.checklist.full blanket-forbids review notes without qualifying by placement, "
            "contradicting prompts/formatter.md which requires them immediately after the metadata "
            f"header. Offending line(s): {unqualified}"
        )

    def test_needs_review_status_is_not_treated_as_a_failure(self):
        """`Status: needs_review` is the formatter correctly ASKING for a human,
        not evidence the phase failed. The checklist must say so.
        """
        checklist = _checklist()
        assert "needs_review" in checklist, (
            "validator.checklist.full never mentions `needs_review`, so the validator is left to "
            "infer that it means the formatter output is incomplete -- which is what failed "
            "production jobs 31, 32, 33, 35 and 36."
        )
        window = re.search(r"[^\n]*needs_review[^\n]*", checklist).group(0)
        assert re.search(
            r"not a (validation )?failure|is not a fail|does not (by itself )?fail", window, re.I
        ), f"validator.checklist.full mentions needs_review but never states it is not a failure: {window!r}"


class TestSeoChecklistJudgesFinalValuesOnly:
    def test_seo_section_scopes_limits_to_final_recommendation(self):
        """The SEO agent shows its revision work (job 35: 109 -> 97 -> 90 chars).
        Only the FINAL recommended value is the deliverable; discarded drafts
        must not be graded as violations.
        """
        section = _section(_checklist(), "SEO Phase")
        assert re.search(r"final|recommend", section, re.I), (
            "SEO Phase checklist does not scope character-limit checks to the FINAL recommended "
            "values, so the validator grades the SEO report's discarded intermediate drafts -- "
            "which is what failed production jobs 31, 34 and 35."
        )


class TestValidatorPromptExampleIsNotEchoed:
    def test_json_example_does_not_ship_a_copyable_formatter_failure(self):
        """The JSON example in prompts/validator.md is few-shot input. Shipping a
        realistic-sounding formatter failure in it invites verbatim echo -- all
        six 2MSY jobs emitted flag text closely tracking this example.
        """
        content = VALIDATOR_PROMPT_PATH.read_text()
        assert not re.search(r"review notes appear in transcript body", content, re.I), (
            "prompts/validator.md's example JSON contains the exact flag text that production "
            "jobs 31-36 reproduced near-verbatim. Use a neutral placeholder instead."
        )


class TestDeterministicLintAgreesWithTheContract:
    """The LLM checklist was not the only place the contract was contradicted.

    ``check_review_notes_placement`` scans for a review-note marker after the
    document's first horizontal rule, and its marker regex matches the bare
    token ``NEEDS_REVIEW`` case-insensitively. But ``prompts/formatter.md``
    REQUIRES a ``**Status:** {ready_for_editing | needs_review}`` footer, and
    that footer sits after a closing ``---`` by contract -- so every
    contract-compliant transcript that asks for human review trips the check.
    """

    def _formatter_doc(self, status: str) -> str:
        return (
            "# Formatted Transcript\n"
            "**Project:** 2MSY0000HD\n\n"
            "<!-- REVIEW NOTES:\n- Something the formatter could not verify\n-->\n\n"
            "---\n\n"
            "**Robert Reed:**  \nSome dialogue that ends properly.\n\n"
            "---\n\n"
            f"**Status:** {status}\n"
        )

    def _violations(self, doc: str):
        from api.services.style_engine.review_notes import check_review_notes_placement

        rules = load_rules(CONFIG_PATH)
        cfg = (rules.raw.get("phases", {}).get("formatter", {}) or {}).get("review_notes") or {}
        return check_review_notes_placement(doc, cfg, "formatter")

    def test_ready_for_editing_footer_is_clean(self):
        """Control: the same document shape with the other status value."""
        assert self._violations(self._formatter_doc("ready_for_editing")) == []

    def test_needs_review_status_footer_is_not_a_misplaced_review_note(self):
        """The regression. `**Status:** needs_review` is the contract's own
        footer, not a review note that drifted into the body.
        """
        found = self._violations(self._formatter_doc("needs_review"))
        assert found == [], (
            "check_review_notes_placement flags the mandatory `**Status:** needs_review` "
            "footer as a review note in the transcript body. Production jobs 31, 32, 33, 35 "
            f"and 36 all carry that footer by contract. Got: {[v.message for v in found]}"
        )

    def test_a_genuinely_misplaced_review_note_is_still_caught(self):
        """Guard against fixing the false positive by gutting the check."""
        doc = (
            "# Formatted Transcript\n**Project:** X\n\n---\n\n"
            "**Speaker:**  \nDialogue.\n\n"
            "<!-- REVIEW NOTES: this one really is in the body -->\n\n"
            "More dialogue.\n\n---\n\n**Status:** ready_for_editing\n"
        )
        assert self._violations(doc), "a review note inside the body must still be flagged"
