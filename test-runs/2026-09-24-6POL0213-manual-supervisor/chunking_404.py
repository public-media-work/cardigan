"""Transcript chunking for parallel formatter processing.

Splits long transcripts into chunks that can be processed concurrently,
then merges the formatted outputs back into a single document.

Short transcripts (<threshold) bypass chunking entirely.
"""

import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from api.services.utils import SRTCaption, generate_srt, parse_srt

logger = logging.getLogger(__name__)

# Default config values (overridden by llm-config.json routing.chunking)
DEFAULT_CHUNKING_CONFIG = {
    "enabled": True,
    "threshold_words": 3000,
    "target_chunk_words": 1500,
    "overlap_captions": 5,
    "max_parallel": 3,
    # Projected output tokens per source word. Measured 1.77 on lyric-dense
    # content (job 48); rounded up because under-chunking truncates while
    # over-chunking only costs a little latency.
    "tokens_per_word": 2.0,
    # Headroom for the metadata header, speaker labels, and ratio variance.
    "safety_factor": 0.8,
}


@dataclass
class TranscriptChunk:
    """A portion of a transcript for parallel processing."""

    index: int
    content: str  # Raw SRT or plain text for this chunk
    start_timecode: str  # Display timecode for logging
    end_timecode: str
    word_count: int
    overlap_prefix: str = ""  # Context from previous chunk's tail


def _count_dialogue_words_srt(captions: List[SRTCaption]) -> int:
    """Count dialogue words across SRT captions (text only, no timecodes)."""
    return sum(len(c.text.split()) for c in captions)


def output_word_budget(
    max_output_tokens: int,
    tokens_per_word: float = 2.0,
    safety_factor: float = 0.8,
) -> int:
    """How many source words one call can format within its output cap.

    Chunking exists to keep each call's OUTPUT inside the model's completion
    limit, so the gate belongs in those units. A word-count threshold could not
    express it: a sparse hour-long program fell under the threshold while a
    dense short one sailed past, and neither fact said anything about whether
    the output would fit (#404).

    Deriving the budget from the cap makes the two move together — raise
    ``max_tokens`` and chunking backs off; lower it and chunking engages.
    """
    return int(max_output_tokens / tokens_per_word * safety_factor)


# How far past the word target to scan for a natural chunk boundary.
_TURN_LOOKAHEAD = 25  # captions — prefer ending right before a new speaker turn
_SENTENCE_LOOKAHEAD = 10  # captions — fallback: sentence-ending punctuation


def _starts_new_turn(caption: SRTCaption) -> bool:
    """True if this caption begins a new speaker turn.

    ``split_interior_speaker_changes`` runs before chunking on SRT input, so
    every ``>>`` marker is guaranteed caption-leading by the time we split —
    a leading ``>>`` reliably marks a new turn. Scripted transcripts with no
    ``>>`` never match; the caller then falls back to sentence boundaries.
    """
    return caption.text.lstrip().startswith(">>")


def _choose_break_idx(captions: List[SRTCaption], i: int) -> int:
    """Pick the caption index to end the current chunk on, at/after ``i``.

    Preference order, so a chunk seam never lands mid-speaker-turn (which is
    what forces the next chunk to re-guess who is speaking and drives the
    speaker-label breakdown at seams):

    1. End at ``j`` where the *next* caption opens a new speaker turn (``>>``),
       so the following chunk starts cleanly on a fresh turn.
    2. Else the first sentence-ending caption (``.?!``) within a shorter window.
    3. Else a hard break at ``i``.
    """
    n = len(captions)
    turn_hi = min(i + _TURN_LOOKAHEAD, n)
    for j in range(i, turn_hi):
        if j + 1 < n and _starts_new_turn(captions[j + 1]):
            return j
    sentence_hi = min(i + _SENTENCE_LOOKAHEAD, n)
    for j in range(i, sentence_hi):
        text = captions[j].text.strip()
        if text and text[-1] in ".?!":
            return j
    return i


def _split_srt(
    content: str,
    target_chunk_words: int,
    overlap_captions: int,
) -> Optional[List[TranscriptChunk]]:
    """Split SRT content into chunks at speaker-turn boundaries.

    Walks captions accumulating word count. Once past the target it picks a
    break via ``_choose_break_idx`` — preferring to end just before a new
    speaker turn (``>>``), falling back to sentence-ending punctuation — so a
    seam never splits a single speaker's turn across two chunks.
    """
    captions = parse_srt(content)
    if not captions:
        return None

    total_words = _count_dialogue_words_srt(captions)
    if total_words <= target_chunk_words:
        # Fits in a single chunk. Previously ``< target * 1.5``, which skipped
        # transcripts between 1x and 1.5x the target — genuinely oversized work
        # that then went out as one call (#404).
        return None

    chunks: List[TranscriptChunk] = []
    chunk_start_idx = 0
    accumulated_words = 0

    i = 0
    while i < len(captions):
        caption_words = len(captions[i].text.split())
        accumulated_words += caption_words

        if accumulated_words >= target_chunk_words and i < len(captions) - 1:
            # Prefer a speaker-turn boundary; fall back to a sentence boundary.
            break_idx = _choose_break_idx(captions, i)

            # Build this chunk's captions
            chunk_captions = captions[chunk_start_idx : break_idx + 1]

            # Build overlap prefix from previous chunk's tail
            overlap = ""
            if chunks and overlap_captions > 0:
                overlap_start = max(chunk_start_idx - overlap_captions, 0)
                overlap_caps = captions[overlap_start:chunk_start_idx]
                if overlap_caps:
                    overlap = generate_srt(overlap_caps)

            chunk = TranscriptChunk(
                index=len(chunks),
                content=generate_srt(chunk_captions),
                start_timecode=chunk_captions[0].start_timecode,
                end_timecode=chunk_captions[-1].end_timecode,
                word_count=_count_dialogue_words_srt(chunk_captions),
                overlap_prefix=overlap,
            )
            chunks.append(chunk)

            chunk_start_idx = break_idx + 1
            accumulated_words = 0
            i = break_idx + 1
            continue

        i += 1

    # Don't forget the final chunk
    if chunk_start_idx < len(captions):
        remaining = captions[chunk_start_idx:]
        overlap = ""
        if chunks and overlap_captions > 0:
            overlap_start = max(chunk_start_idx - overlap_captions, 0)
            overlap_caps = captions[overlap_start:chunk_start_idx]
            if overlap_caps:
                overlap = generate_srt(overlap_caps)

        chunk = TranscriptChunk(
            index=len(chunks),
            content=generate_srt(remaining),
            start_timecode=remaining[0].start_timecode,
            end_timecode=remaining[-1].end_timecode,
            word_count=_count_dialogue_words_srt(remaining),
            overlap_prefix=overlap,
        )
        chunks.append(chunk)

    if len(chunks) <= 1:
        return None

    return chunks


def _split_plain_text(
    content: str,
    target_chunk_words: int,
) -> Optional[List[TranscriptChunk]]:
    """Split plain text at paragraph boundaries.

    Last 2 paragraphs of each chunk become overlap for the next.
    """
    paragraphs = re.split(r"\n\s*\n", content.strip())
    paragraphs = [p.strip() for p in paragraphs if p.strip()]

    if not paragraphs:
        return None

    total_words = sum(len(p.split()) for p in paragraphs)
    if total_words <= target_chunk_words:
        # Fits in a single chunk (see the SRT splitter's note on the old 1.5x guard).
        return None

    chunks: List[TranscriptChunk] = []
    current_paragraphs: List[str] = []
    current_words = 0

    for para in paragraphs:
        para_words = len(para.split())
        current_paragraphs.append(para)
        current_words += para_words

        if current_words >= target_chunk_words:
            chunk_text = "\n\n".join(current_paragraphs)

            # Build overlap from last 2 paragraphs
            overlap = ""
            if chunks:
                # Get last 2 paragraphs from previous chunk
                prev_text = chunks[-1].content
                prev_paras = re.split(r"\n\s*\n", prev_text.strip())
                overlap_paras = prev_paras[-2:] if len(prev_paras) >= 2 else prev_paras
                overlap = "\n\n".join(overlap_paras)

            chunk = TranscriptChunk(
                index=len(chunks),
                content=chunk_text,
                start_timecode="",
                end_timecode="",
                word_count=current_words,
                overlap_prefix=overlap,
            )
            chunks.append(chunk)

            current_paragraphs = []
            current_words = 0

    # Final chunk
    if current_paragraphs:
        chunk_text = "\n\n".join(current_paragraphs)
        overlap = ""
        if chunks:
            prev_text = chunks[-1].content
            prev_paras = re.split(r"\n\s*\n", prev_text.strip())
            overlap_paras = prev_paras[-2:] if len(prev_paras) >= 2 else prev_paras
            overlap = "\n\n".join(overlap_paras)

        chunk = TranscriptChunk(
            index=len(chunks),
            content=chunk_text,
            start_timecode="",
            end_timecode="",
            word_count=current_words,
            overlap_prefix=overlap,
        )
        chunks.append(chunk)

    if len(chunks) <= 1:
        return None

    return chunks


def split_transcript(
    content: str,
    is_srt: bool,
    config: Optional[Dict] = None,
    max_output_tokens: Optional[int] = None,
) -> Optional[List[TranscriptChunk]]:
    """Split a transcript into chunks for parallel processing.

    Returns None if the transcript fits one call or only produces one chunk —
    the caller then uses the normal single-call path.

    Args:
        content: Raw transcript content (SRT or plain text)
        is_srt: Whether the content is SRT format
        config: Chunking config from llm-config.json routing.chunking
        max_output_tokens: The backend's completion cap. When given, the gate
            and the chunk target both derive from it (see output_word_budget),
            replacing the static word thresholds. When omitted, the legacy
            ``threshold_words``/``target_chunk_words`` behavior applies.

    Returns:
        List of TranscriptChunk if chunking applies, None otherwise
    """
    cfg = {**DEFAULT_CHUNKING_CONFIG, **(config or {})}

    if not cfg.get("enabled", True):
        return None

    # Count words (dialogue only for SRT)
    if is_srt:
        captions = parse_srt(content)
        word_count = _count_dialogue_words_srt(captions) if captions else 0
    else:
        word_count = len(content.split())

    # Prefer the output-budget gate when the caller knows the cap; fall back to
    # the static thresholds otherwise (#404).
    if max_output_tokens:
        threshold = output_word_budget(
            max_output_tokens,
            tokens_per_word=cfg.get("tokens_per_word", 2.0),
            safety_factor=cfg.get("safety_factor", 0.8),
        )
        target = threshold
    else:
        threshold = cfg["threshold_words"]
        target = cfg["target_chunk_words"]

    if word_count <= threshold:
        # Logged at info: prod previously had no record of WHY chunking was
        # skipped, which is what let #404 hide.
        logger.info(
            "Transcript fits a single call — not chunking",
            extra={
                "word_count": word_count,
                "budget_words": threshold,
                "max_output_tokens": max_output_tokens,
            },
        )
        return None

    overlap = cfg.get("overlap_captions", 5)

    if is_srt:
        chunks = _split_srt(content, target, overlap)
    else:
        chunks = _split_plain_text(content, target)

    if chunks:
        logger.info(
            "Transcript split into chunks",
            extra={
                "chunk_count": len(chunks),
                "word_count": word_count,
                "target_per_chunk": target,
            },
        )

    return chunks


def merge_formatter_chunks(chunks: List[str]) -> str:
    """Merge formatted chunk outputs into a single document.

    1. Keep header (before first ---) from chunk 0 only
    2. Strip headers from chunks 1+
    3. Strip Status line from all but last chunk
    4. Collect review notes into one block at top
    5. Deduplicate overlap at chunk seams
    6. Concatenate

    Args:
        chunks: List of formatted output strings, one per chunk

    Returns:
        Merged formatter output
    """
    if not chunks:
        return ""
    if len(chunks) == 1:
        return chunks[0]

    # Extract all review notes
    review_notes: List[str] = []
    review_pattern = re.compile(r"<!--\s*REVIEW NOTES\s*-->.*?(?=<!--|$)", re.DOTALL | re.IGNORECASE)

    # Process each chunk
    header = ""
    bodies: List[str] = []
    status_line = ""

    for i, chunk in enumerate(chunks):
        # Strip provenance HTML comment from top (<!-- model: ... -->)
        chunk = re.sub(r"^<!--\s*model:.*?-->\s*\n?", "", chunk.strip())

        # Strip LLM conversational preamble before any markdown content
        # (e.g., "I'll now format the complete transcript..." that some models
        # emit before their actual output). Anything before the first markdown
        # structure marker (```, #, **, ---, or <!-- HTML comment) is treated
        # as preamble and removed.
        preamble_strip = re.sub(
            r"\A(?:(?!^(?:```|#\s|\*\*|---|<!--))[^\n]*\n)+",
            "",
            chunk,
            count=1,
            flags=re.MULTILINE,
        )
        chunk = preamble_strip.lstrip("\n")

        # Strip wrapping ```markdown ... ``` code fence if the LLM wrapped its
        # output. We only strip the OUTERMOST fence pair (opening at start,
        # closing at end), so any legitimate fenced code within the dialogue
        # remains intact.
        fence_open = re.match(r"^```(?:markdown|md)?\s*\n", chunk)
        if fence_open:
            chunk = chunk[fence_open.end() :]
            chunk = re.sub(r"\n```\s*\Z", "", chunk.rstrip()) + "\n"

        # Strip LLM-generated model/creator attribution lines (appear at end of chunk responses)
        chunk = re.sub(
            r"^\*\*(?:Model|Creator|Agent):\*\*.*\n?",
            "",
            chunk,
            flags=re.MULTILINE,
        )
        # Clean up orphaned --- separators left after attribution removal
        chunk = re.sub(r"\n---+\s*\n*$", "", chunk.strip())

        # Extract review notes from this chunk
        notes = review_pattern.findall(chunk)
        for note in notes:
            note = note.strip()
            if note and note not in review_notes:
                review_notes.append(note)
        # Remove review notes from chunk body
        chunk = review_pattern.sub("", chunk).strip()

        if i == 0:
            # First chunk: extract header (everything before first ---)
            parts = re.split(r"^---+\s*$", chunk, maxsplit=1, flags=re.MULTILINE)
            if len(parts) > 1:
                header = parts[0].strip()
                body = parts[1].strip()
            else:
                body = chunk
        else:
            # Subsequent chunks: strip any generated header
            body = chunk
            # Remove "# Formatted Transcript" heading
            body = re.sub(r"^#\s+Formatted Transcript\s*\n?", "", body, flags=re.MULTILINE)
            # Remove metadata lines (Project:, Program:, Duration:, Date:)
            body = re.sub(
                r"^\*\*(?:Project|Program|Duration|Date|Air Date|Media ID):\*\*.*\n?",
                "",
                body,
                flags=re.MULTILINE,
            )
            # Remove --- separator at top if present after header removal
            body = re.sub(r"^---+\s*\n?", "", body.strip(), flags=re.MULTILINE)
            body = body.strip()

        # Extract and save Status line from last chunk only
        status_match = re.search(r"^\*\*Status:\*\*\s+.*$", body, flags=re.MULTILINE)
        if status_match:
            if i == len(chunks) - 1:
                status_line = status_match.group(0)
            # Remove status from all chunks
            body = re.sub(r"^\*\*Status:\*\*\s+.*$", "", body, flags=re.MULTILINE).strip()

        bodies.append(body)

    # Deduplicate any echoed overlap at seams (structural, turn-aware).
    for i in range(len(bodies) - 1):
        bodies[i + 1] = _dedup_seam_turns(bodies[i], bodies[i + 1])

    # Build final document
    parts = []

    if header:
        parts.append(header)

    # Add consolidated review notes
    if review_notes:
        notes_block = "<!-- REVIEW NOTES -->\n" + "\n".join(review_notes) + "\n<!-- /REVIEW NOTES -->"
        parts.append(notes_block)

    if header or review_notes:
        parts.append("---")

    parts.append("\n\n".join(b for b in bodies if b))

    if status_line:
        parts.append(status_line)

    return "\n\n".join(parts)


# Max leading turns of a continuation chunk to test for echoed overlap (also the
# window of trailing previous-chunk turns tested against).
_MAX_SEAM_ECHO_TURNS = 6

# A speaker turn starts at a line-leading bold label, e.g. ``**Jane Doe:**``.
_TURN_SPLIT_RE = re.compile(r"(?m)(?=^\*\*[^*\n]+?:\*\*)")
_TURN_LABEL_RE = re.compile(r"^\*\*[^*\n]+?:\*\*[ \t]*")


def _split_into_turns(body: str) -> List[str]:
    """Split a formatted transcript body into speaker-turn blocks.

    A turn runs from a line-leading ``**Speaker:**`` label to the next such
    label. Any text before the first label is kept as a leading block so
    nothing is dropped, and whitespace inside each block is preserved verbatim.
    """
    return [part for part in _TURN_SPLIT_RE.split(body) if part.strip()]


def _turn_dialogue_key(turn: str) -> str:
    """Whitespace/label-normalized dialogue of a turn, for echo comparison."""
    without_label = _TURN_LABEL_RE.sub("", turn.strip(), count=1)
    return re.sub(r"\s+", " ", without_label).strip().lower()


def _dedup_seam_turns(prev_body: str, next_body: str) -> str:
    """Drop leading turns of ``next_body`` that echo the tail of ``prev_body``.

    Replaces the previous fuzzy word-window trim (which flattened structure and
    could over-trim real dialogue). It works on whole speaker turns, so it never
    flattens, never trims mid-label, and never deletes a partial line.

    An echo is modelled precisely: the model re-emitting the input overlap means
    ``next_body`` OPENS with a contiguous run of turns identical (whitespace-
    normalized) to the turns that CLOSE ``prev_body`` — a suffix of prev
    appearing as a prefix of next. Only that seam-adjacent contiguous run is
    dropped (the largest match, up to ``_MAX_SEAM_ECHO_TURNS``). Anchoring on the
    exact seam — rather than matching a leading turn against *any* recent turn —
    avoids misclassifying a short turn that merely repeats one said a few turns
    earlier (e.g. a "Right." backchannel) as an echo. Because ``_split_srt``
    partitions the source captions with no content overlap, a seam-adjacent
    contiguous duplicate is an echo, not new dialogue.
    """
    prev_turns = _split_into_turns(prev_body)
    next_turns = _split_into_turns(next_body)
    if not prev_turns or not next_turns:
        return next_body

    prev_keys = [_turn_dialogue_key(t) for t in prev_turns]
    next_keys = [_turn_dialogue_key(t) for t in next_turns]
    max_k = min(len(prev_turns), len(next_turns), _MAX_SEAM_ECHO_TURNS)

    drop = 0
    for k in range(max_k, 0, -1):
        prev_tail = prev_keys[-k:]
        if all(prev_tail) and prev_tail == next_keys[:k]:  # non-empty contiguous match
            drop = k
            break

    if not drop:
        return next_body

    logger.info("Dropped echoed turn(s) at chunk seam", extra={"turns": drop})
    return "".join(next_turns[drop:]).lstrip("\n")
