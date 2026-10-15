#!/usr/bin/env python3
"""Turn an SRT or SCC caption file into plain text for keyword extraction.

Cardigan already parses SRT (``api/services/utils.py:parse_srt``), but nothing
in the codebase decodes SCC -- mmingest fetches and stores ``.scc`` sidecars
without ever reading them. This module fills that gap for the tag-scout skill
and is deliberately dependency-free so it can be vendored into cardigan proper
if the spike graduates.

Two jobs beyond plain parsing:

1. **SCC decoding.** SCC is a Scenarist container around a raw EIA-608 byte
   stream: ``HH:MM:SS;FF<TAB>`` followed by space-separated hex byte pairs.
   Bytes carry an odd-parity bit that must be masked off, control codes are
   transmitted twice, and the character set is *not* ASCII above 0x7A.

2. **Rolling-window de-duplication.** Live-captioned SRTs (the common case for
   Here & Now and anything else captioned in real time) emit the entire visible
   2-3 line window in every cue, so each line of dialogue appears 2-3 times.
   Feeding that to a keyword extractor inflates term frequency and biases
   ranking toward whatever happened to sit mid-window. ``merge_captions``
   collapses the overlap on word boundaries.

Usage:
    captions_to_text.py FILE [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# EIA-608 character tables
# ---------------------------------------------------------------------------

# The basic set is ASCII 0x20-0x7F with nine substitutions. Getting these wrong
# is silent: you get '}' where the caption said 'N-tilde'.
_BASIC_OVERRIDES = {
    0x2A: "á",
    0x5C: "é",
    0x5E: "í",
    0x5F: "ó",
    0x60: "ú",
    0x7B: "ç",
    0x7C: "÷",
    0x7D: "Ñ",
    0x7E: "ñ",
    0x7F: "█",
}

# Special North American set: control pair 0x11 0x30-0x3F.
_SPECIAL_NA = {
    0x30: "®",
    0x31: "°",
    0x32: "½",
    0x33: "¿",
    0x34: "™",
    0x35: "¢",
    0x36: "£",
    0x37: "♪",
    0x38: "à",
    0x39: " ",
    0x3A: "è",
    0x3B: "â",
    0x3C: "ê",
    0x3D: "î",
    0x3E: "ô",
    0x3F: "û",
}

# Extended Spanish/French: control pair 0x12 0x20-0x3F.
_EXT_SPANISH_FRENCH = {
    0x20: "Á",
    0x21: "É",
    0x22: "Ó",
    0x23: "Ú",
    0x24: "Ü",
    0x25: "ü",
    0x26: "'",
    0x27: "¡",
    0x28: "*",
    0x29: "'",
    0x2A: "─",
    0x2B: "©",
    0x2C: "℠",
    0x2D: "·",
    0x2E: "“",
    0x2F: "”",
    0x30: "À",
    0x31: "Â",
    0x32: "Ç",
    0x33: "È",
    0x34: "Ê",
    0x35: "Ë",
    0x36: "ë",
    0x37: "Î",
    0x38: "Ï",
    0x39: "ï",
    0x3A: "Ô",
    0x3B: "Ù",
    0x3C: "ù",
    0x3D: "Û",
    0x3E: "«",
    0x3F: "»",
}

# Extended Portuguese/German/Danish: control pair 0x13 0x20-0x3F.
_EXT_PORTUGUESE_GERMAN = {
    0x20: "Ã",
    0x21: "ã",
    0x22: "Í",
    0x23: "Ì",
    0x24: "ì",
    0x25: "Ò",
    0x26: "ò",
    0x27: "Õ",
    0x28: "õ",
    0x29: "{",
    0x2A: "}",
    0x2B: "\\",
    0x2C: "^",
    0x2D: "_",
    0x2E: "|",
    0x2F: "~",
    0x30: "Ä",
    0x31: "ä",
    0x32: "Ö",
    0x33: "ö",
    0x34: "ß",
    0x35: "¥",
    0x36: "¤",
    0x37: "│",
    0x38: "Å",
    0x39: "å",
    0x3A: "Ø",
    0x3B: "ø",
    0x3C: "┌",
    0x3D: "┐",
    0x3E: "└",
    0x3F: "┘",
}

# Miscellaneous control codes (first byte 0x14/0x15, second byte below).
_EOC = 0x2F  # End of caption -- flip non-displayed memory to screen (pop-on)
_CR = 0x2D  # Carriage return -- roll the display up one line (roll-up)

_SCC_LINE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})[:;](\d{2})\s*\t\s*(.+)$")
_SRT_TIME = re.compile(r"(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})")
_MARKUP_TAG = re.compile(r"</?[a-zA-Z][^>]*>")


@dataclass
class Caption:
    """One caption cue. ``end_ms`` is None for SCC, which carries no end times."""

    start_ms: int
    text: str
    end_ms: Optional[int] = None


@dataclass
class Extraction:
    format: str
    text: str
    duration_ms: int
    cue_count: int
    live_captioning: bool


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


def detect_format(content: str) -> str:
    """Return "scc" or "srt" based on content, not file extension.

    Raises ValueError if the content is neither.
    """
    head = content.lstrip()[:4000]
    if head.lower().startswith("scenarist_scc"):
        return "scc"
    if _SRT_TIME.search(head):
        return "srt"
    # Headerless SCC: a drop-frame timecode followed by a tab and hex pairs.
    for line in head.splitlines():
        if _SCC_LINE.match(line.rstrip()):
            return "scc"
    raise ValueError("Content is neither SRT nor SCC (no SRT timecode arrow, no SCC hex line)")


# ---------------------------------------------------------------------------
# SCC
# ---------------------------------------------------------------------------


def _scc_timecode_to_ms(hh: str, mm: str, ss: str, ff: str) -> int:
    # 29.97 drop-frame is the overwhelming norm for broadcast SCC. Frame-level
    # precision does not matter here -- nothing downstream uses it for cueing.
    return ((int(hh) * 3600) + (int(mm) * 60) + int(ss)) * 1000 + int(int(ff) * (1000 / 29.97))


def _decode_char(byte: int) -> str:
    if byte < 0x20:
        return ""  # null padding and control bytes carry no text
    return _BASIC_OVERRIDES.get(byte, chr(byte))


def decode_scc(content: str) -> list[Caption]:
    """Decode an SCC file's EIA-608 stream into caption cues (CC1 only)."""
    captions: list[Caption] = []
    buffer: list[str] = []
    pending_start: Optional[int] = None

    def flush(at_ms: Optional[int]) -> None:
        nonlocal buffer, pending_start
        text = re.sub(r"\s+", " ", "".join(buffer)).strip()
        if text:
            start = pending_start if pending_start is not None else (at_ms or 0)
            captions.append(Caption(start_ms=start, text=text))
        buffer = []
        pending_start = None

    for raw_line in content.splitlines():
        match = _SCC_LINE.match(raw_line.rstrip())
        if not match:
            continue
        hh, mm, ss, ff, payload = match.groups()
        line_ms = _scc_timecode_to_ms(hh, mm, ss, ff)

        prev_control: Optional[tuple[int, int]] = None
        for token in payload.split():
            try:
                word = int(token, 16)
            except ValueError:
                continue
            # Mask the odd-parity bit off both bytes.
            b1 = (word >> 8) & 0x7F
            b2 = word & 0x7F

            if 0x10 <= b1 <= 0x1F:
                # Control pair. 0x18-0x1F addresses the second data channel
                # (CC2/CC4); we decode CC1 only.
                if b1 >= 0x18:
                    prev_control = None
                    continue
                # Control codes are transmitted twice for error resilience.
                if (b1, b2) == prev_control:
                    prev_control = None
                    continue
                prev_control = (b1, b2)

                if b1 == 0x11 and 0x30 <= b2 <= 0x3F:
                    if pending_start is None:
                        pending_start = line_ms
                    buffer.append(_SPECIAL_NA[b2])
                elif b1 in (0x12, 0x13) and 0x20 <= b2 <= 0x3F:
                    # Extended chars arrive *after* a basic-set approximation
                    # that the decoder is required to discard.
                    if buffer:
                        buffer.pop()
                    if pending_start is None:
                        pending_start = line_ms
                    table = _EXT_SPANISH_FRENCH if b1 == 0x12 else _EXT_PORTUGUESE_GERMAN
                    buffer.append(table[b2])
                elif b1 in (0x14, 0x15) and b2 in (_EOC, _CR):
                    flush(line_ms)
                elif 0x40 <= b2 <= 0x7F:
                    # Preamble address code: moves the cursor to a new row.
                    # Within a caption that is a line break, not a flush.
                    if buffer:
                        buffer.append(" ")
                elif b1 == 0x11 and 0x20 <= b2 <= 0x2F:
                    # Mid-row style code (color/italic/underline) renders as a space.
                    if buffer:
                        buffer.append(" ")
                # Every other control code (RCL, ENM, EDM, RU2/3/4, TAB, ...)
                # affects presentation only and carries no text.
                continue

            prev_control = None
            chars = _decode_char(b1) + _decode_char(b2)
            if chars:
                if pending_start is None:
                    pending_start = line_ms
                buffer.append(chars)

    flush(None)
    return captions


# ---------------------------------------------------------------------------
# SRT
# ---------------------------------------------------------------------------


def _srt_timecode_to_ms(timecode: str) -> int:
    clock, _, frac = timecode.replace(".", ",").partition(",")
    hh, mm, ss = clock.split(":")
    return ((int(hh) * 3600) + (int(mm) * 60) + int(ss)) * 1000 + int(frac.ljust(3, "0"))


def parse_srt(content: str) -> list[Caption]:
    """Parse SRT cues. Malformed blocks are skipped, never fatal.

    Unlike ``api.services.utils.parse_srt`` this joins a cue's lines with a
    space rather than a newline -- the output here is prose for a keyword
    extractor, not a caption file to be re-rendered.
    """
    captions: list[Caption] = []
    for block in re.split(r"\n\s*\n", content.strip()):
        lines = [line for line in block.strip().splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        time_line_index = 0 if _SRT_TIME.search(lines[0]) else 1
        if time_line_index >= len(lines):
            continue
        match = _SRT_TIME.search(lines[time_line_index])
        if not match:
            continue
        text = _MARKUP_TAG.sub("", " ".join(lines[time_line_index + 1 :]))
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        captions.append(
            Caption(
                start_ms=_srt_timecode_to_ms(match.group(1)),
                text=text,
                end_ms=_srt_timecode_to_ms(match.group(2)),
            )
        )
    return captions


# ---------------------------------------------------------------------------
# Rolling-window merge
# ---------------------------------------------------------------------------


def merge_captions(captions: list[Caption]) -> str:
    """Join cues into prose, collapsing repeated rolling-window text.

    For each cue, find the longest word-aligned suffix of what we have so far
    that is also a prefix of the incoming cue, and append only the remainder.
    An overlap of a single word is ignored unless it consumes the whole cue --
    otherwise an ordinary sentence ending in "the" followed by a cue starting
    with "the" would silently lose a word.
    """
    merged: list[str] = []
    for caption in captions:
        words = re.sub(r"\s+", " ", caption.text).strip().split()
        if not words:
            continue
        overlap = 0
        for size in range(min(len(merged), len(words)), 0, -1):
            if merged[-size:] == words[:size]:
                overlap = size
                break
        if overlap == 1 and overlap != len(words):
            overlap = 0
        merged.extend(words[overlap:])
    return " ".join(merged)


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------


def _detect_live_captioning(captions: list[Caption]) -> bool:
    """Flag live/real-time captioning per analyst.md's tells.

    ``>>`` speaker-change markers in place of named speakers is the reliable
    one. The skill uses this to suppress name-guessing: reconstructing a proper
    noun from garbled live-caption phonetics produces confident, wrong tags.
    """
    if not captions:
        return False
    marked = sum(1 for c in captions if ">>" in c.text)
    return marked >= 2 or (marked >= 1 and marked / len(captions) >= 0.05)


def extract(content: str) -> Extraction:
    fmt = detect_format(content)
    captions = decode_scc(content) if fmt == "scc" else parse_srt(content)
    if captions:
        last = captions[-1]
        duration_ms = last.end_ms if last.end_ms is not None else last.start_ms
    else:
        duration_ms = 0
    return Extraction(
        format=fmt,
        text=merge_captions(captions),
        duration_ms=duration_ms,
        cue_count=len(captions),
        live_captioning=_detect_live_captioning(captions),
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Extract plain text from an SRT or SCC caption file.")
    parser.add_argument("file", help="Path to a .srt or .scc file")
    parser.add_argument("--json", action="store_true", help="Emit JSON metadata alongside the text")
    args = parser.parse_args(argv)

    try:
        with open(args.file, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # SCC is ASCII; SRT is usually UTF-8 but Windows-1252 turns up often enough.
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            content = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        print("error: could not decode file as text", file=sys.stderr)
        return 1

    try:
        result = extract(content)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(
            json.dumps(
                {
                    "format": result.format,
                    "duration_ms": result.duration_ms,
                    "duration_minutes": round(result.duration_ms / 60000, 1),
                    "cue_count": result.cue_count,
                    "live_captioning": result.live_captioning,
                    "word_count": len(result.text.split()),
                    "text": result.text,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        minutes = round(result.duration_ms / 60000, 1)
        print(
            f"# format={result.format} duration={minutes}min cues={result.cue_count} "
            f"words={len(result.text.split())} live_captioning={result.live_captioning}",
            file=sys.stderr,
        )
        print(result.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
