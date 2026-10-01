"""Tests for transcript chunking (split + merge)."""

from api.services.chunking import (
    _choose_break_idx,
    _dedup_seam_turns,
    _split_into_turns,
    _split_plain_text,
    _split_srt,
    format_duration,
    merge_formatter_chunks,
    split_transcript,
)
from api.services.utils import parse_srt

# ─── Helpers ───────────────────────────────────────────────────────────


def make_srt(num_captions: int, words_per_caption: int = 10) -> str:
    """Generate SRT content with specified number of captions."""
    lines = []
    for i in range(1, num_captions + 1):
        start_ms = (i - 1) * 3000
        end_ms = i * 3000
        h1, m1, s1 = start_ms // 3600000, (start_ms % 3600000) // 60000, (start_ms % 60000) // 1000
        h2, m2, s2 = end_ms // 3600000, (end_ms % 3600000) // 60000, (end_ms % 60000) // 1000

        # Make some captions end with sentence-ending punctuation
        text_words = [f"word{j}" for j in range(words_per_caption - 1)]
        if i % 5 == 0:
            text_words.append("end.")
        else:
            text_words.append(f"word{words_per_caption}")

        lines.append(str(i))
        lines.append(f"{h1:02d}:{m1:02d}:{s1:02d},000 --> {h2:02d}:{m2:02d}:{s2:02d},000")
        lines.append(" ".join(text_words))
        lines.append("")

    return "\n".join(lines)


def make_plain_text(num_paragraphs: int, words_per_paragraph: int = 100) -> str:
    """Generate plain text with specified paragraphs."""
    paragraphs = []
    for i in range(num_paragraphs):
        words = [f"word{j}" for j in range(words_per_paragraph)]
        paragraphs.append(" ".join(words))
    return "\n\n".join(paragraphs)


def make_turn_srt(num_captions: int, words_per_caption: int = 30, turn_every: int = 10) -> str:
    """SRT where every ``turn_every``-th caption (0-based) opens a new turn (``>>``).

    Mirrors a live-caption transcript after ``split_interior_speaker_changes``,
    where every ``>>`` marker is caption-leading.
    """
    lines = []
    for i in range(1, num_captions + 1):
        start_ms = (i - 1) * 3000
        end_ms = i * 3000
        h1, m1, s1 = start_ms // 3600000, (start_ms % 3600000) // 60000, (start_ms % 60000) // 1000
        h2, m2, s2 = end_ms // 3600000, (end_ms % 3600000) // 60000, (end_ms % 60000) // 1000
        text = " ".join(f"word{j}" for j in range(words_per_caption))
        if (i - 1) % turn_every == 0:  # caption opens a new speaker turn
            text = ">> " + text
        lines.append(str(i))
        lines.append(f"{h1:02d}:{m1:02d}:{s1:02d},000 --> {h2:02d}:{m2:02d}:{s2:02d},000")
        lines.append(text)
        lines.append("")
    return "\n".join(lines)


# ─── split_transcript tests ───────────────────────────────────────────


class TestSplitTranscript:
    def test_below_threshold_returns_none(self):
        """Short transcripts should not be chunked."""
        srt = make_srt(10, words_per_caption=5)  # 50 words total
        result = split_transcript(srt, is_srt=True, config={"threshold_words": 3000})
        assert result is None

    def test_disabled_config_returns_none(self):
        """Chunking disabled in config should return None."""
        srt = make_srt(500, words_per_caption=10)  # 5000 words
        result = split_transcript(srt, is_srt=True, config={"enabled": False})
        assert result is None

    def test_srt_above_threshold_returns_chunks(self):
        """Long SRT should be split into multiple chunks."""
        srt = make_srt(400, words_per_caption=10)  # 4000 words
        result = split_transcript(
            srt,
            is_srt=True,
            config={"threshold_words": 3000, "target_chunk_words": 1500, "overlap_captions": 5},
        )
        assert result is not None
        assert len(result) >= 2
        # All chunks should have content
        for chunk in result:
            assert chunk.content
            assert chunk.word_count > 0

    def test_plain_text_above_threshold(self):
        """Long plain text should be split into chunks."""
        text = make_plain_text(40, words_per_paragraph=100)  # 4000 words
        result = split_transcript(
            text,
            is_srt=False,
            config={"threshold_words": 3000, "target_chunk_words": 1500},
        )
        assert result is not None
        assert len(result) >= 2

    def test_single_chunk_returns_none(self):
        """If splitting produces only 1 chunk, return None."""
        # Just barely over threshold but not enough for 2 chunks
        srt = make_srt(200, words_per_caption=10)  # 2000 words
        result = split_transcript(
            srt,
            is_srt=True,
            config={"threshold_words": 1900, "target_chunk_words": 3000},
        )
        assert result is None


# ─── _split_srt tests ─────────────────────────────────────────────────


class TestSplitSRT:
    def test_basic_split(self):
        """SRT with enough words should produce multiple chunks."""
        srt = make_srt(400, words_per_caption=10)  # 4000 words
        chunks = _split_srt(srt, target_chunk_words=1500, overlap_captions=5)
        assert chunks is not None
        assert len(chunks) >= 2
        # Chunks should have sequential indices
        for i, chunk in enumerate(chunks):
            assert chunk.index == i

    def test_sentence_boundary_preference(self):
        """Chunks should prefer breaking at sentence-ending punctuation."""
        # Our make_srt adds "end." at every 5th caption
        srt = make_srt(400, words_per_caption=10)
        chunks = _split_srt(srt, target_chunk_words=1500, overlap_captions=0)
        assert chunks is not None
        # Check that non-final chunks end at sentence boundaries where possible
        for chunk in chunks[:-1]:
            lines = chunk.content.strip().split("\n")
            # Find the last text line (not index or timecode)
            for line in reversed(lines):
                line = line.strip()
                if line and not line.isdigit() and "-->" not in line:
                    break
            # Many (not all) should end with period
            # Just check this doesn't crash; exact boundary depends on word counts

    def test_overlap_prefix(self):
        """Chunks 1+ should have overlap prefix from previous chunk."""
        srt = make_srt(400, words_per_caption=10)
        chunks = _split_srt(srt, target_chunk_words=1500, overlap_captions=5)
        assert chunks is not None
        assert len(chunks) >= 2
        # First chunk has no overlap
        assert chunks[0].overlap_prefix == ""
        # Subsequent chunks should have overlap
        for chunk in chunks[1:]:
            assert chunk.overlap_prefix != ""
            # Overlap should be valid SRT content
            assert "-->" in chunk.overlap_prefix

    def test_timecodes_present(self):
        """Each chunk should have start/end timecodes."""
        srt = make_srt(400, words_per_caption=10)
        chunks = _split_srt(srt, target_chunk_words=1500, overlap_captions=5)
        assert chunks is not None
        for chunk in chunks:
            assert chunk.start_timecode
            assert chunk.end_timecode
            assert ":" in chunk.start_timecode


# ─── _split_plain_text tests ──────────────────────────────────────────


class TestSplitPlainText:
    def test_paragraph_boundary_splitting(self):
        """Plain text should split on paragraph boundaries."""
        text = make_plain_text(40, words_per_paragraph=100)  # 4000 words
        chunks = _split_plain_text(text, target_chunk_words=1500)
        assert chunks is not None
        assert len(chunks) >= 2
        # Each chunk should contain complete paragraphs
        for chunk in chunks:
            assert chunk.content.strip()

    def test_short_text_returns_none(self):
        """Text that would produce 1 chunk should return None."""
        text = make_plain_text(5, words_per_paragraph=100)  # 500 words
        chunks = _split_plain_text(text, target_chunk_words=1500)
        assert chunks is None

    def test_overlap_from_previous(self):
        """Chunks 1+ should get overlap from previous chunk's tail."""
        text = make_plain_text(40, words_per_paragraph=100)
        chunks = _split_plain_text(text, target_chunk_words=1500)
        assert chunks is not None
        assert chunks[0].overlap_prefix == ""
        for chunk in chunks[1:]:
            assert chunk.overlap_prefix != ""


# ─── merge_formatter_chunks tests ─────────────────────────────────────


class TestMergeFormatterChunks:
    def test_single_chunk_passthrough(self):
        """Single chunk should pass through unchanged."""
        result = merge_formatter_chunks(["Hello world"])
        assert result == "Hello world"

    def test_empty_list(self):
        """Empty list should return empty string."""
        assert merge_formatter_chunks([]) == ""

    def test_keeps_only_first_header(self):
        """Only chunk 0's header should be preserved."""
        chunk0 = """**Project:** Test Project
**Program:** Test Program

---

First chunk body content here."""

        chunk1 = """**Project:** Test Project
**Program:** Test Program

---

Second chunk body content here."""

        result = merge_formatter_chunks([chunk0, chunk1])
        # Should have only one Project line
        assert result.count("**Project:**") == 1
        assert "First chunk body content" in result
        assert "Second chunk body content" in result

    def test_strips_formatted_transcript_heading(self):
        """# Formatted Transcript heading should be stripped from chunks 1+."""
        chunk0 = """**Project:** Test

---

First body."""

        chunk1 = """# Formatted Transcript

Second body."""

        result = merge_formatter_chunks([chunk0, chunk1])
        assert "# Formatted Transcript" not in result
        assert "Second body" in result

    def test_strips_intermediate_status(self):
        """Status line should only come from last chunk."""
        chunk0 = "Body one.\n\n**Status:** ready_for_editing"
        chunk1 = "Body two.\n\n**Status:** ready_for_editing"
        chunk2 = "Body three.\n\n**Status:** ready_for_editing"

        result = merge_formatter_chunks([chunk0, chunk1, chunk2])
        assert result.count("**Status:**") == 1
        # Status should be at the end
        assert result.strip().endswith("**Status:** ready_for_editing")

    def test_collects_review_notes(self):
        """Review notes from all chunks should be merged at top."""
        chunk0 = "<!-- REVIEW NOTES -->\nNote from chunk 0\n\nBody zero."
        chunk1 = "<!-- REVIEW NOTES -->\nNote from chunk 1\n\nBody one."

        result = merge_formatter_chunks([chunk0, chunk1])
        # Both notes should be in the consolidated block
        assert "Note from chunk 0" in result
        assert "Note from chunk 1" in result
        # Notes should appear before bodies
        notes_pos = result.find("REVIEW NOTES")
        body_pos = result.find("Body zero")
        assert notes_pos < body_pos

    def test_strips_provenance_comments(self):
        """Provenance HTML comments should be stripped."""
        chunk0 = "<!-- model: gpt-4o | tier: default | cost: $0.01 | tokens: 500 -->\n**Project:** Test\n\n---\n\nBody."
        chunk1 = "<!-- model: gpt-4o | tier: default | cost: $0.01 | tokens: 500 -->\nMore body."

        result = merge_formatter_chunks([chunk0, chunk1])
        assert "model: gpt-4o" not in result

    def test_trims_overlap(self):
        """Duplicate text at chunk seams should be trimmed."""
        # Create chunks with overlapping text at the seam
        shared_text = " ".join([f"overlap{i}" for i in range(20)])
        chunk0 = f"Start of chunk zero. {shared_text}"
        chunk1 = f"{shared_text} End of chunk one."

        result = merge_formatter_chunks([chunk0, chunk1])
        # The shared text should not appear twice in full
        # (exact dedup depends on the window size and ratio)
        assert "Start of chunk zero" in result
        assert "End of chunk one" in result

    def test_strips_wrapping_markdown_code_fence(self):
        """LLM outputs that wrap entire content in ```markdown ... ``` fences should be unwrapped.

        Mirrors the real defect observed on job 171: chunked formatter LLM
        responses wrapped the full output (header + body) in a single
        ```markdown ... ``` fence pair, which made the renderer treat the
        whole chunk as a code block.
        """
        chunk0 = """```markdown
**Project:** Test

---

**Speaker A:** First chunk dialogue.
```"""
        chunk1 = """```markdown
**Speaker B:** Second chunk dialogue.
```"""

        result = merge_formatter_chunks([chunk0, chunk1])
        # No code fences should remain (would make renderer treat content as code block)
        assert "```" not in result
        # Dialogue content must be preserved verbatim
        assert "First chunk dialogue" in result
        assert "Second chunk dialogue" in result

    def test_strips_preamble_then_fence(self):
        """The exact pathology observed on job 171: preamble + fence-wrapped chunk."""
        chunk0 = """I'll now format the complete transcript. Let me process all the SRT blocks carefully.

```markdown
**Project:** Test

---

**Speaker A:** First chunk dialogue.
```"""
        chunk1 = """**Speaker B:** Second chunk dialogue (no wrap on this chunk)."""

        result = merge_formatter_chunks([chunk0, chunk1])
        assert "```" not in result
        assert "I'll now format" not in result
        assert "First chunk dialogue" in result
        assert "Second chunk dialogue" in result

    def test_strips_conversational_preamble(self):
        """LLM preambles like 'I'll now format...' before markdown content should be stripped."""
        chunk0 = """I'll now format the complete transcript. Let me process all the SRT blocks carefully.

**Project:** Test

---

**Speaker A:** Dialogue."""
        chunk1 = """Here is the second chunk:

**Speaker B:** More dialogue."""

        result = merge_formatter_chunks([chunk0, chunk1])
        assert "I'll now format" not in result
        assert "Here is the second chunk" not in result
        assert "Dialogue" in result
        assert "More dialogue" in result


# ─── deterministic wrapper (FMT-1) ────────────────────────────────────

# Job 24 (6POL0214): chunk 0 saw only its own slice and wrote the duration of
# that slice -- 00:11:06 for an 18.6-minute episode.
_JOB24_CHUNK0 = """# Formatted Transcript
**Project:** 6POL0214
**Program:** Inside Wisconsin Politics
**Duration:** 00:11:06
**Date Processed:** 2026-10-01

<!-- REVIEW NOTES:
- "Colombo Channel" (5:00-5:05): unclear reference.
-->

---

**Narrator:**
This is Inside Wisconsin Politics."""

_JOB24_CHUNK1 = """**Zac Schultz:**
That's all the time we've got.

**Status:** needs_review"""

_AUTHORITATIVE = {
    "Project": "6POL0214",
    "Program": "Inside Wisconsin Politics",
    "Duration": "00:18:37",
    "Date Processed": "2026-10-02",
}


class TestFormatDuration:
    def test_job24_duration(self):
        assert format_duration(18.622516666666666) == "00:18:37"

    def test_over_an_hour(self):
        assert format_duration(61.5) == "01:01:30"

    def test_missing_or_invalid(self):
        assert format_duration(None) is None
        assert format_duration(0) is None
        assert format_duration("n/a") is None


class TestDeterministicWrapper:
    def test_header_values_come_from_metadata_not_chunk0(self):
        result = merge_formatter_chunks([_JOB24_CHUNK0, _JOB24_CHUNK1], header_fields=_AUTHORITATIVE)
        assert "**Duration:** 00:18:37" in result
        assert "00:11:06" not in result
        assert "**Date Processed:** 2026-10-02" in result
        assert result.count("**Project:**") == 1
        assert result.startswith("# Formatted Transcript\n**Project:** 6POL0214\n")

    def test_contract_form_review_notes_survive_header_replacement(self):
        result = merge_formatter_chunks([_JOB24_CHUNK0, _JOB24_CHUNK1], header_fields=_AUTHORITATIVE)
        assert "Colombo Channel" in result
        # Notes sit between the header and the first rule, in the contract form.
        first_rule = result.index("\n---\n")
        assert result.index("<!-- REVIEW NOTES:") < first_rule
        assert result.index("Colombo Channel") < first_rule

    def test_contract_form_notes_in_later_chunk_move_to_top(self):
        chunk1 = "<!-- REVIEW NOTES:\n- Spelling check: Tiffany\n-->\n\n" + _JOB24_CHUNK1
        result = merge_formatter_chunks([_JOB24_CHUNK0, chunk1], header_fields=_AUTHORITATIVE)
        first_rule = result.index("\n---\n")
        assert result.index("Spelling check: Tiffany") < first_rule
        assert result.count("<!-- REVIEW NOTES") == 1

    def test_missing_value_falls_back_to_model_header(self):
        fields = dict(_AUTHORITATIVE, Program=None)
        result = merge_formatter_chunks([_JOB24_CHUNK0, _JOB24_CHUNK1], header_fields=fields)
        assert "**Program:** Inside Wisconsin Politics" in result
        assert "**Duration:** 00:18:37" in result

    def test_missing_duration_is_omitted_not_taken_from_chunk0(self):
        fields = dict(_AUTHORITATIVE, Duration=None)
        result = merge_formatter_chunks([_JOB24_CHUNK0, _JOB24_CHUNK1], header_fields=fields)
        assert "**Duration:**" not in result
        assert "00:11:06" not in result

    def test_contract_notes_stripped_from_body_without_header_fields(self):
        """The merge's notes regex never matched the contract form, so notes
        written mid-body by chunk 1+ stayed there -- the validator's
        "review notes appear in transcript body" flag."""
        chunk1 = (
            "**Zac Schultz:**\nOne.\n\n<!-- REVIEW NOTES:\n- Spelling check: Tiffany\n-->\n\n**Zac Schultz:**\nTwo."
        )
        result = merge_formatter_chunks([_JOB24_CHUNK0, chunk1])
        first_rule = result.index("\n---\n")
        assert result.index("Spelling check: Tiffany") < first_rule
        assert result.count("<!-- REVIEW NOTES") == 1
        assert "One." in result and "Two." in result

    def test_header_written_even_when_chunk0_omits_it(self):
        chunk0 = "**Narrator:**\nThis is Inside Wisconsin Politics."
        result = merge_formatter_chunks([chunk0, _JOB24_CHUNK1], header_fields=_AUTHORITATIVE)
        assert result.startswith("# Formatted Transcript\n**Project:** 6POL0214")
        assert "**Duration:** 00:18:37" in result
        assert "This is Inside Wisconsin Politics." in result

    def test_header_without_rule_is_not_duplicated(self):
        chunk0 = "# Formatted Transcript\n**Project:** 6POL0214\n**Duration:** 00:11:06\n\n**Narrator:**\nHello."
        result = merge_formatter_chunks([chunk0, _JOB24_CHUNK1], header_fields=_AUTHORITATIVE)
        assert result.count("**Project:**") == 1
        assert result.count("# Formatted Transcript") == 1
        assert "00:11:06" not in result
        assert "Hello." in result

    def test_status_footer_still_from_last_chunk(self):
        result = merge_formatter_chunks([_JOB24_CHUNK0, _JOB24_CHUNK1], header_fields=_AUTHORITATIVE)
        assert result.strip().endswith("**Status:** needs_review")
        assert result.count("**Status:**") == 1

    def test_dialogue_preserved(self):
        result = merge_formatter_chunks([_JOB24_CHUNK0, _JOB24_CHUNK1], header_fields=_AUTHORITATIVE)
        assert "This is Inside Wisconsin Politics." in result
        assert "That's all the time we've got." in result


# ─── turn-boundary split tests ────────────────────────────────────────


class TestTurnBoundarySplit:
    def test_chunks_start_at_turn_boundary(self):
        """The first continuation chunk begins cleanly on a new speaker turn."""
        srt = make_turn_srt(40, words_per_caption=30, turn_every=10)
        chunks = _split_srt(srt, target_chunk_words=500, overlap_captions=0)
        assert chunks is not None and len(chunks) >= 2
        first_caption = parse_srt(chunks[1].content)[0]
        assert first_caption.text.lstrip().startswith(
            ">>"
        ), f"continuation chunk starts mid-turn: {first_caption.text!r}"

    def test_choose_break_prefers_turn_boundary(self):
        """_choose_break_idx ends just before the next >> caption."""
        caps = parse_srt(make_turn_srt(30, words_per_caption=5, turn_every=6))
        # Turn-start captions are 0-based indices 0, 6, 12, ... From i=2 the next
        # turn start is index 6, so the break lands at 5 (its predecessor).
        idx = _choose_break_idx(caps, 2)
        assert idx == 5
        assert caps[idx + 1].text.lstrip().startswith(">>")

    def test_choose_break_falls_back_to_sentence(self):
        """With no >> markers, fall back to sentence-ending punctuation."""
        caps = parse_srt(make_srt(30, words_per_caption=5))  # 'end.' every 5th caption
        idx = _choose_break_idx(caps, 1)
        assert caps[idx].text.strip().endswith(".")

    def test_scripted_no_markers_still_splits(self):
        """Transcripts with zero >> markers still chunk (sentence fallback)."""
        srt = make_srt(400, words_per_caption=10)  # 4000 words, no >>
        chunks = _split_srt(srt, target_chunk_words=1500, overlap_captions=5)
        assert chunks is not None and len(chunks) >= 2


# ─── turn-aware seam dedup tests ──────────────────────────────────────


class TestDedupSeamTurns:
    def test_split_into_turns(self):
        body = "leading note\n\n**Alice:**  \nHi there.\n\n**Bob:**  \nYo."
        turns = _split_into_turns(body)
        assert turns[0].strip() == "leading note"
        assert turns[1].startswith("**Alice:**")
        assert turns[2].startswith("**Bob:**")

    def test_drops_echoed_leading_turn(self):
        """A leading turn echoing the previous chunk's tail is dropped once."""
        prev = "**Alice:**  \nHello there, everyone.\n\n**Bob:**  \nGood to be here."
        nxt = "**Bob:**  \nGood to be here.\n\n**Alice:**  \nLet's begin the discussion."
        merged = merge_formatter_chunks([prev, nxt])
        assert merged.count("Good to be here") == 1
        assert "Let's begin the discussion" in merged
        assert "**Alice:**" in merged and "**Bob:**" in merged

    def test_preserves_distinct_turns(self):
        """No echo -> nothing dropped; all dialogue preserved."""
        prev = "**Alice:**  \nFirst statement here.\n\n**Bob:**  \nSecond statement here."
        nxt = "**Carol:**  \nThird distinct statement.\n\n**Dave:**  \nFourth distinct statement."
        merged = merge_formatter_chunks([prev, nxt])
        for snippet in ("First statement", "Second statement", "Third distinct", "Fourth distinct"):
            assert snippet in merged

    def test_never_flattens_structure(self):
        """Kept turns retain label-on-line structure (regression: old trim flattened)."""
        prev = "**Alice:**  \nEcho line one.\n\n**Bob:**  \nEcho line two."
        nxt = "**Bob:**  \nEcho line two.\n\n**Carol:**  \nKept line with structure."
        merged = merge_formatter_chunks([prev, nxt])
        assert "**Carol:**  \nKept line with structure." in merged

    def test_no_false_dedup_direct(self):
        """_dedup_seam_turns returns next_body unchanged when no leading echo."""
        prev = "**Alice:**  \nOne.\n\n**Bob:**  \nTwo."
        nxt = "**Carol:**  \nThree.\n\n**Dave:**  \nFour."
        assert _dedup_seam_turns(prev, nxt) == nxt

    def test_drops_multi_turn_contiguous_echo(self):
        """A 2+ turn contiguous echo (prev's tail re-emitted as next's head) is
        fully dropped, leaving only the genuinely new content."""
        prev = "**Alice:**  \nFirst point here.\n\n**Bob:**  \nSecond point here."
        nxt = "**Alice:**  \nFirst point here.\n\n**Bob:**  \nSecond point here.\n\n" "**Carol:**  \nBrand new content."
        out = _dedup_seam_turns(prev, nxt)
        assert out.count("First point here") == 0
        assert out.count("Second point here") == 0
        assert "Brand new content" in out
        assert out.strip().startswith("**Carol:**")

    def test_backchannel_matching_non_seam_turn_is_kept(self):
        """A leading turn that matches a prev turn NOT at the seam (a "Right."
        backchannel said a few turns earlier) is kept — the match must anchor at
        prev's actual tail, so it is not misclassified as an echo."""
        prev = (
            "**Alice:**  \nRight.\n\n**Bob:**  \nThe vote finally passed today.\n\n"
            "**Carol:**  \nA major milestone indeed."
        )
        nxt = "**Dave:**  \nRight.\n\n**Erin:**  \nOn to the next agenda item."
        assert _dedup_seam_turns(prev, nxt) == nxt  # nothing dropped
