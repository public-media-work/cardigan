"""Utility functions for Cardigan API.

Provides timezone-aware datetime handling, SRT parsing, and common utilities.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# Filename Sanitization
# =============================================================================

# Patterns for OS-generated duplicate file suffixes
# These should be stripped to normalize Media IDs
# ORDER MATTERS: More specific patterns must come before generic ones
DUPLICATE_FILE_PATTERNS = [
    # Windows: "file - Copy.txt", "file - Copy (2).txt"
    # Must come BEFORE macOS pattern to catch "- Copy (2)" as one unit
    re.compile(r"\s*-\s*Copy(?:\s*\((\d+)\))?\s*$", re.IGNORECASE),
    # Generic: "file copy.txt", "file copy 2.txt"
    re.compile(r"\s+copy(?:\s+(\d+))?\s*$", re.IGNORECASE),
    # macOS: "file (1).txt", "file (2).txt", etc.
    # Must come AFTER Windows pattern to avoid partial matches
    re.compile(r"\s*\((\d+)\)\s*$"),
    # macOS alternate: "file 2.txt" (less common, only match if digit at very end)
    # Note: Commented out to avoid false positives with legitimate IDs like "WEB02"
    # re.compile(r'\s+(\d+)\s*$'),
]


def sanitize_duplicate_filename(filename: str) -> Tuple[str, bool]:
    """Remove OS-generated duplicate suffixes from filename.

    Detects patterns like "(1)", "- Copy", "copy 2" that operating systems
    add when saving duplicate files. These patterns should be stripped
    to normalize Media IDs.

    IMPORTANT: This does NOT strip legitimate PBS naming patterns like:
    - _REV20251022 (revision dates)
    - _SM, HD, WEB02 (segment/format markers)
    - _midshow, _excerpt (position markers)

    Args:
        filename: Filename or Media ID (with or without extension)

    Returns:
        Tuple of (sanitized_name, was_duplicate):
        - sanitized_name: Filename with duplicate suffix removed
        - was_duplicate: True if a duplicate pattern was found and removed

    Examples:
        >>> sanitize_duplicate_filename("2WLIComicArtistSM (1)")
        ('2WLIComicArtistSM', True)
        >>> sanitize_duplicate_filename("2WLI1209HD - Copy")
        ('2WLI1209HD', True)
        >>> sanitize_duplicate_filename("9UNP2005HD copy 2")
        ('9UNP2005HD', True)
        >>> sanitize_duplicate_filename("2WLI1209HD_REV20251022")
        ('2WLI1209HD_REV20251022', False)
        >>> sanitize_duplicate_filename("2WLI1209HD")
        ('2WLI1209HD', False)
    """
    original = filename
    was_duplicate = False

    for pattern in DUPLICATE_FILE_PATTERNS:
        match = pattern.search(filename)
        if match:
            # Found a duplicate pattern - remove it
            filename = pattern.sub("", filename).strip()
            was_duplicate = True
            logger.warning(f"Detected duplicate file suffix in '{original}' -> " f"normalized to '{filename}'")
            break  # Only remove one pattern (they shouldn't stack)

    return filename, was_duplicate


def reset_phases_for_reprocess(phases, skip: Tuple[str, ...] = ()) -> List[dict]:
    """Reset phases to pending for reprocessing, archiving completed runs.

    Shared by transcript replacement and transcript-review approval. Phases
    named in ``skip`` (e.g. a completed transcription stage) are preserved
    as-is. A phase that recorded a model gets its run archived into
    ``previous_runs`` before the reset.

    Args:
        phases: List of JobPhase models (or dicts) from a Job.
        skip: Phase names to leave untouched.

    Returns:
        List of phase dicts ready for JobPhase(**d).
    """
    reset = []
    for phase in phases or []:
        phase_dict = phase.model_dump() if hasattr(phase, "model_dump") else dict(phase)
        if phase_dict.get("name") in skip:
            reset.append(phase_dict)
            continue

        if phase_dict.get("model"):
            prev_run = {
                "model": phase_dict.get("model"),
                "cost": phase_dict.get("cost", 0),
                "tokens": phase_dict.get("tokens", 0),
                "completed_at": phase_dict.get("completed_at"),
            }
            previous_runs = phase_dict.get("previous_runs") or []
            previous_runs.append(prev_run)
            phase_dict["previous_runs"] = previous_runs

        phase_dict["status"] = "pending"
        phase_dict["completed_at"] = None
        phase_dict["error_message"] = None
        phase_dict["cost"] = 0
        phase_dict["tokens"] = 0
        phase_dict["model"] = None
        phase_dict["metadata"] = None
        reset.append(phase_dict)
    return reset


def utc_now() -> datetime:
    """Return current UTC time as timezone-aware datetime.

    This function should be used instead of the deprecated datetime.utcnow(),
    which returns naive datetime objects.

    Returns:
        Timezone-aware datetime representing the current UTC time

    Examples:
        >>> now = utc_now()
        >>> now.tzinfo is not None
        True
        >>> now.tzinfo == timezone.utc
        True
    """
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    """Return current UTC time as ISO 8601 formatted string.

    Returns:
        ISO 8601 string representation of current UTC time with timezone info

    Examples:
        >>> timestamp = utc_now_iso()
        >>> timestamp.endswith('+00:00') or timestamp.endswith('Z')
        True
    """
    return datetime.now(timezone.utc).isoformat()


def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Convert naive datetime to UTC-aware datetime.

    If the datetime is already timezone-aware, returns it unchanged.
    If the datetime is naive (no timezone), assumes it represents UTC
    and adds UTC timezone information.

    Args:
        dt: Datetime to convert (can be None)

    Returns:
        Timezone-aware datetime or None if input is None

    Examples:
        >>> from datetime import datetime
        >>> naive_dt = datetime(2024, 1, 15, 12, 30, 0)
        >>> aware_dt = ensure_utc(naive_dt)
        >>> aware_dt.tzinfo == timezone.utc
        True

        >>> already_aware = datetime(2024, 1, 15, 12, 30, 0, tzinfo=timezone.utc)
        >>> ensure_utc(already_aware) == already_aware
        True

        >>> ensure_utc(None) is None
        True
    """
    if dt is None:
        return None

    if dt.tzinfo is None:
        # Naive datetime - assume UTC and add timezone info
        return dt.replace(tzinfo=timezone.utc)

    # Already timezone-aware - return unchanged
    return dt


def parse_iso_datetime(s: str) -> datetime:
    """Parse ISO 8601 datetime string to UTC-aware datetime.

    Handles various ISO 8601 formats and ensures the result is always
    in UTC with timezone information.

    Args:
        s: ISO 8601 formatted datetime string

    Returns:
        Timezone-aware datetime in UTC

    Raises:
        ValueError: If string cannot be parsed as ISO datetime

    Examples:
        >>> dt = parse_iso_datetime("2024-01-15T12:30:00+00:00")
        >>> dt.tzinfo == timezone.utc
        True

        >>> dt = parse_iso_datetime("2024-01-15T12:30:00Z")
        >>> dt.tzinfo == timezone.utc
        True

        >>> dt = parse_iso_datetime("2024-01-15T12:30:00")
        >>> dt.tzinfo == timezone.utc
        True
    """
    try:
        # Try parsing with fromisoformat (handles most ISO formats)
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"Invalid ISO datetime string: {s}") from e

    # Ensure result is UTC-aware
    return ensure_utc(dt)


def calculate_transcript_metrics(
    transcript_content: str,
    words_per_minute: int = 150,
    long_form_threshold_minutes: int = 15,
    is_srt: bool = False,
) -> dict:
    """Calculate metrics from transcript content for routing decisions.

    Args:
        transcript_content: Raw transcript text
        words_per_minute: Speaking rate estimate (default 150 wpm)
        long_form_threshold_minutes: Minutes threshold for long-form classification
        is_srt: Count spoken words only, skipping SRT index numbers and
            timecodes. Without this a naive whitespace split counts ~4 markup
            tokens per caption as speech, inflating word_count by the caption
            count (job 48 reported 3750 against 2318 real words) (#404).

    Returns:
        Dict with word_count, estimated_duration_minutes, is_long_form

    Examples:
        >>> metrics = calculate_transcript_metrics("Hello world " * 1000)
        >>> metrics["word_count"]
        2000
        >>> metrics["estimated_duration_minutes"]  # 2000 / 150 = 13.33
        13.33
        >>> metrics["is_long_form"]
        False

        >>> metrics = calculate_transcript_metrics("Hello world " * 2500)
        >>> metrics["is_long_form"]  # 5000 words / 150 wpm = 33.33 min
        True
    """
    # Count words. SRT markup is not speech, so parse it out when we know the
    # content is SRT; fall back to the whitespace split if parsing finds nothing.
    word_count = 0
    if is_srt:
        captions = parse_srt(transcript_content)
        if captions:
            word_count = sum(len(c.text.split()) for c in captions)
    if not word_count:
        word_count = len(transcript_content.split())

    # Estimate duration based on speaking rate
    estimated_duration_minutes = round(word_count / words_per_minute, 2)

    # Classify as long-form if exceeds threshold
    is_long_form = estimated_duration_minutes > long_form_threshold_minutes

    return {
        "word_count": word_count,
        "estimated_duration_minutes": estimated_duration_minutes,
        "is_long_form": is_long_form,
    }


def extract_media_id(filename: str) -> Optional[str]:
    """Extract PBS Wisconsin Media ID from transcript filename using regex.

    Searches the filename for a valid PBS Media ID pattern:
    4 alphanumeric chars (program code) + 4 digits (episode) + optional letter suffix.

    Processing order:
    1. Strip macOS/Windows duplicate suffixes (e.g. " (1)", " - Copy")
    2. Strip _ForClaude suffix
    3. Match PBS Media ID pattern; if followed by _REV[date], include it
    4. Fall back to full sanitized stem for project-style names (no spaces)
    5. Return None if stem contains spaces or no valid pattern found

    Args:
        filename: Transcript filename (with or without extension)

    Returns:
        Extracted media ID, or None if filename doesn't contain a valid pattern.

    Examples:
        >>> extract_media_id("2WLI1209HD_ForClaude.txt")
        '2WLI1209HD'
        >>> extract_media_id("9UNP2005HD.srt")
        '9UNP2005HD'
        >>> extract_media_id("2BUC0000HDWEB02_REV20251202.srt")
        '2BUC0000HDWEB02_REV20251202'
        >>> extract_media_id("2WLI1210HD_midshow.srt")
        '2WLI1210HD'
        >>> extract_media_id("6GWQ2503_REV20251121.srt")
        '6GWQ2503_REV20251121'
        >>> extract_media_id("WC_S01_trailer.srt")
        'WC_S01_trailer'
        >>> extract_media_id("TLB CC IN WI.srt")
        >>> extract_media_id("test_transcript.txt")
        'test_transcript'
    """
    stem = Path(filename).stem

    # Step 1: Strip macOS/Windows duplicate suffixes (e.g. " (1)", " - Copy")
    sanitized, _ = sanitize_duplicate_filename(stem)

    # Step 2: Strip _ForClaude (case-insensitive), may appear mid-stem before _REV
    sanitized = re.sub(r"_ForClaude", "", sanitized, flags=re.IGNORECASE)

    # Step 3: Match PBS Media ID pattern
    # e.g., 2WLI1209HD, 6GWQ2503, 2BUC0000HDWEB02
    # Reject matches starting with "REV" — those are revision date suffixes
    # (e.g., _REV20251106 falsely matching REV20251 as a show code)
    match = re.search(r"([A-Z0-9]{4}\d{4}(?:[A-Z]+\d{0,2})?)", sanitized, re.IGNORECASE)
    if match and not match.group(1).upper().startswith("REV"):
        base_id = match.group(1).upper()
        # Check if a _REV[date] suffix follows the matched base ID in the sanitized stem
        rev_match = re.search(
            r"([A-Z0-9]{4}\d{4}(?:[A-Z]+\d{0,2})?)(_REV\d+)",
            sanitized,
            re.IGNORECASE,
        )
        if rev_match and not rev_match.group(1).upper().startswith("REV"):
            return (rev_match.group(1) + rev_match.group(2)).upper()
        return base_id

    # Step 4: Fall back to the sanitized stem for project-style names
    # Only accept names with no spaces (underscore-separated or plain identifiers)
    if " " not in sanitized:
        return sanitized if sanitized else None

    # Step 5: Stem contains spaces — freeform text, not a valid Media ID
    return None


# =============================================================================
# SRT Parsing Utilities
# =============================================================================


@dataclass
class SRTCaption:
    """Represents a single SRT caption entry."""

    index: int
    start_ms: int  # Start time in milliseconds
    end_ms: int  # End time in milliseconds
    text: str  # Caption text (may be multiline)

    @property
    def start_timecode(self) -> str:
        """Return start time as SRT timecode (HH:MM:SS,mmm)."""
        return ms_to_srt_timecode(self.start_ms)

    @property
    def end_timecode(self) -> str:
        """Return end time as SRT timecode (HH:MM:SS,mmm)."""
        return ms_to_srt_timecode(self.end_ms)

    @property
    def duration_ms(self) -> int:
        """Return duration in milliseconds."""
        return self.end_ms - self.start_ms

    def to_srt(self) -> str:
        """Convert to SRT format string."""
        return f"{self.index}\n{self.start_timecode} --> {self.end_timecode}\n{self.text}\n"

    def to_vtt(self) -> str:
        """Convert to WebVTT format string (no index, period for ms)."""
        start_vtt = ms_to_vtt_timecode(self.start_ms)
        end_vtt = ms_to_vtt_timecode(self.end_ms)
        return f"{start_vtt} --> {end_vtt}\n{self.text}\n"


def srt_timecode_to_ms(timecode: str) -> int:
    """Convert SRT timecode string to milliseconds.

    Args:
        timecode: SRT format timecode "HH:MM:SS,mmm"

    Returns:
        Total milliseconds

    Examples:
        >>> srt_timecode_to_ms("00:00:01,500")
        1500
        >>> srt_timecode_to_ms("01:30:45,123")
        5445123
        >>> srt_timecode_to_ms("00:02:30,000")
        150000
    """
    # Handle both , and . as millisecond separator
    timecode = timecode.replace(".", ",")
    match = re.match(r"(\d{1,2}):(\d{2}):(\d{2}),(\d{3})", timecode.strip())
    if not match:
        raise ValueError(f"Invalid SRT timecode: {timecode}")

    hours, minutes, seconds, ms = map(int, match.groups())
    total_ms = (hours * 3600 + minutes * 60 + seconds) * 1000 + ms
    return total_ms


def ms_to_srt_timecode(ms: int) -> str:
    """Convert milliseconds to SRT timecode string.

    Args:
        ms: Total milliseconds

    Returns:
        SRT format timecode "HH:MM:SS,mmm"

    Examples:
        >>> ms_to_srt_timecode(1500)
        '00:00:01,500'
        >>> ms_to_srt_timecode(5445123)
        '01:30:45,123'
        >>> ms_to_srt_timecode(150000)
        '00:02:30,000'
    """
    if ms < 0:
        ms = 0

    hours = ms // 3600000
    ms %= 3600000
    minutes = ms // 60000
    ms %= 60000
    seconds = ms // 1000
    milliseconds = ms % 1000

    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def ms_to_vtt_timecode(ms: int) -> str:
    """Convert milliseconds to WebVTT timecode string.

    Args:
        ms: Total milliseconds

    Returns:
        WebVTT format timecode "HH:MM:SS.mmm"

    Examples:
        >>> ms_to_vtt_timecode(1500)
        '00:00:01.500'
        >>> ms_to_vtt_timecode(5445123)
        '01:30:45.123'
    """
    srt_tc = ms_to_srt_timecode(ms)
    return srt_tc.replace(",", ".")


def ms_to_display_timecode(ms: int, include_hours: bool = False) -> str:
    """Convert milliseconds to display-friendly timecode.

    Args:
        ms: Total milliseconds
        include_hours: Always include hours, even if 0

    Returns:
        Display format "MM:SS" or "H:MM:SS"

    Examples:
        >>> ms_to_display_timecode(150000)
        '02:30'
        >>> ms_to_display_timecode(150000, include_hours=True)
        '0:02:30'
        >>> ms_to_display_timecode(5445000)
        '1:30:45'
    """
    if ms < 0:
        ms = 0

    total_seconds = ms // 1000
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60

    if hours > 0 or include_hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    else:
        return f"{minutes:02d}:{seconds:02d}"


def parse_srt(content: str) -> List[SRTCaption]:
    """Parse SRT file content into list of caption objects.

    Args:
        content: Full SRT file content as string

    Returns:
        List of SRTCaption objects in order

    Raises:
        ValueError: If content cannot be parsed

    Examples:
        >>> srt = '''1
        ... 00:00:01,000 --> 00:00:03,000
        ... Hello world
        ...
        ... 2
        ... 00:00:04,000 --> 00:00:06,000
        ... Second caption
        ... '''
        >>> captions = parse_srt(srt)
        >>> len(captions)
        2
        >>> captions[0].text
        'Hello world'
    """
    captions = []

    # Split into blocks (separated by blank lines)
    blocks = re.split(r"\n\s*\n", content.strip())

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        lines = block.split("\n")
        if len(lines) < 3:
            continue

        try:
            # Line 1: Index number
            index = int(lines[0].strip())

            # Line 2: Timecodes
            time_match = re.match(
                r"(\d{1,2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,\.]\d{3})", lines[1].strip()
            )
            if not time_match:
                continue

            start_ms = srt_timecode_to_ms(time_match.group(1))
            end_ms = srt_timecode_to_ms(time_match.group(2))

            # Lines 3+: Caption text
            text = "\n".join(lines[2:]).strip()

            captions.append(SRTCaption(index=index, start_ms=start_ms, end_ms=end_ms, text=text))
        except (ValueError, IndexError):
            # Skip malformed entries
            continue

    return captions


def generate_srt(captions: List[SRTCaption]) -> str:
    """Generate SRT file content from list of captions.

    Args:
        captions: List of SRTCaption objects

    Returns:
        Complete SRT file content as string
    """
    # Renumber captions sequentially
    output_parts = []
    for i, caption in enumerate(captions, 1):
        caption.index = i
        output_parts.append(caption.to_srt())

    return "\n".join(output_parts)


def generate_vtt(captions: List[SRTCaption]) -> str:
    """Generate WebVTT file content from list of captions.

    Args:
        captions: List of SRTCaption objects

    Returns:
        Complete WebVTT file content as string
    """
    output_parts = ["WEBVTT", ""]

    for caption in captions:
        output_parts.append(caption.to_vtt())

    return "\n".join(output_parts)


def clean_srt_captions(
    captions: List[SRTCaption], min_gap_ms: int = 50, max_duration_ms: int = 7000, merge_threshold_ms: int = 1000
) -> List[SRTCaption]:
    """Clean and normalize SRT captions.

    Fixes common issues:
    - Removes duplicate consecutive captions
    - Fixes overlapping timecodes
    - Merges very short captions
    - Ensures minimum gap between captions

    Args:
        captions: List of SRTCaption objects
        min_gap_ms: Minimum gap between captions (default 50ms)
        max_duration_ms: Maximum caption duration (default 7000ms)
        merge_threshold_ms: Merge captions shorter than this (default 1000ms)

    Returns:
        Cleaned list of SRTCaption objects
    """
    if not captions:
        return []

    cleaned = []
    prev_caption = None

    for caption in captions:
        # Skip empty captions
        if not caption.text.strip():
            continue

        # Skip duplicates
        if prev_caption and caption.text.strip() == prev_caption.text.strip():
            # Extend previous caption's end time if needed
            if caption.end_ms > prev_caption.end_ms:
                prev_caption.end_ms = caption.end_ms
            continue

        # Fix negative duration
        if caption.end_ms <= caption.start_ms:
            caption.end_ms = caption.start_ms + 1000  # Default 1 second

        # Fix overlaps with previous caption
        if prev_caption and caption.start_ms < prev_caption.end_ms + min_gap_ms:
            # Adjust start time to maintain minimum gap
            caption.start_ms = prev_caption.end_ms + min_gap_ms

        # Merge very short captions with previous if possible
        if (
            prev_caption and caption.duration_ms < merge_threshold_ms and caption.start_ms - prev_caption.end_ms < 500
        ):  # Close in time
            # Merge into previous caption
            prev_caption.text = prev_caption.text + "\n" + caption.text
            prev_caption.end_ms = caption.end_ms
            continue

        cleaned.append(caption)
        prev_caption = caption

    # Renumber
    for i, caption in enumerate(cleaned, 1):
        caption.index = i

    return cleaned


def get_srt_duration(captions: List[SRTCaption]) -> int:
    """Get total duration from captions in milliseconds.

    Args:
        captions: List of SRTCaption objects

    Returns:
        End time of last caption in milliseconds
    """
    if not captions:
        return 0
    return max(c.end_ms for c in captions)
