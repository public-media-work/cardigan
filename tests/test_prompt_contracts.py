"""Drift tests: the phase prompts must not contradict each other or the checklist.

Half the defects found in the 2026-09-24 supervised run of 6POL0213 were not
code bugs at all — they were two documents disagreeing about the same contract,
with the validator penalising the formatter for obeying its own prompt. These
tests read the REAL production prompts and pin the agreements that run proved
matter. They are deliberately about the CONTRACT, not about wording: each one
names the disagreement it prevents.
"""

import re
from pathlib import Path

import yaml

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
HOUSE_STYLE = Path(__file__).resolve().parent.parent / "config" / "house_style.yaml"


def _validator_checklist() -> str:
    data = yaml.safe_load(HOUSE_STYLE.read_text())

    def walk(node):
        if isinstance(node, str):
            return node
        if isinstance(node, dict):
            return "\n".join(walk(v) for v in node.values())
        if isinstance(node, list):
            return "\n".join(walk(v) for v in node)
        return ""

    return walk(data).lower()


class TestReviewNotesContract:
    """VAL-1: the formatter's own template puts an optional REVIEW NOTES block
    directly after the header. The validator checklist said 'no review notes
    appear in the transcript body', so a formatter doing exactly as instructed
    was flagged. 23 of 28 paused prod jobs carried a review-notes flag."""

    def test_formatter_template_marks_review_notes_optional(self):
        text = (PROMPTS / "formatter.md").read_text()
        assert "REVIEW NOTES (only if needed)" in text

    def test_validator_checklist_allows_the_block_after_the_header(self):
        checklist = _validator_checklist()
        assert "review notes" in checklist, "checklist must still speak to review notes"
        assert "designated" in checklist or "legal there" in checklist, (
            "the checklist must state that a REVIEW NOTES block after the header is "
            "legal, or it will keep flagging the formatter for following its template"
        )

    def test_validator_scopes_the_violation_to_the_dialogue(self):
        checklist = _validator_checklist()
        assert "dialogue" in checklist, (
            "only review notes leaking into the DIALOGUE are a violation; without "
            "that scoping the rule reads as a blanket ban"
        )


class TestSeoStatusVocabulary:
    """VAL-2: seo.md defines {draft | ready_for_review | approved} as the legal
    set; the validator flagged 'draft' as incomplete work (job 44)."""

    def test_seo_prompt_defines_the_status_set(self):
        assert "{draft | ready_for_review | approved}" in (PROMPTS / "seo.md").read_text()

    def test_validator_knows_all_three_are_legal(self):
        checklist = _validator_checklist()
        for value in ("draft", "ready_for_review", "approved"):
            assert value in checklist, f"validator checklist must accept {value!r} as legal"


class TestFormatterSpeakerLabels:
    """PROMPT-2: formatter.md forbids parenthetical speaker labels, then used
    them in its own examples. Opus flagged the contradiction and improvised;
    Sonnet copied the examples."""

    def test_no_parenthetical_speaker_labels_in_examples(self):
        text = (PROMPTS / "formatter.md").read_text()
        # A speaker label is bold text ending in a colon. Flag any that carry a
        # parenthetical, except lines explicitly marked as counter-examples.
        offenders = []
        for line in text.splitlines():
            if not re.search(r"\*\*[^*]+\([^)]+\)[^*]*:\*\*", line):
                continue
            if any(marker in line for marker in ("❌", "not ", "WRONG", "Do NOT", "do not use")):
                continue  # deliberate counter-example
            offenders.append(line.strip())
        assert not offenders, "formatter.md examples must obey its own no-parentheticals rule: " + "; ".join(offenders)


class TestSeoDraftingTrail:
    """SEO-1 (#309): the character-count narration invited working text into the
    deliverable — 'Still 130 chars…' shipped inside a description."""

    def test_seo_prompt_forbids_revision_history(self):
        text = (PROMPTS / "seo.md").read_text().lower()
        assert "final" in text and "do not report character counts" in text.replace("do not", "do not")


class TestAnalystTimings:
    """AN-1: act timings were invented out to 30:30 on an 18:04 episode, in the
    same document that stated the true span."""

    def test_analyst_caps_timings_at_actual_duration(self):
        # Normalised: the instruction is a wrapped markdown blockquote, so raw
        # substring matching would break on line wrapping rather than on meaning.
        raw = (PROMPTS / "analyst.md").read_text().lower()
        text = re.sub(r"[\s>]+", " ", raw)
        assert "srt timecodes" in text
        assert "never past it" in text, "the analyst must be told not to invent timings past the episode end"
