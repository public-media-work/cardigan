"""Tests for SRT-based duration calculation at ingest."""

import pytest

from api.services.utils import get_srt_duration, parse_srt

SAMPLE_SRT = """1
00:00:00,000 --> 00:00:04,200
Welcome to the show.

2
00:00:04,200 --> 00:00:11,800
Today we're discussing something important.

3
00:17:55,000 --> 00:18:08,500
Thanks for watching, see you next time.
"""


def test_get_srt_duration_returns_last_timestamp():
    """SRT duration should be the end time of the last caption."""
    captions = parse_srt(SAMPLE_SRT)
    duration_ms = get_srt_duration(captions)
    assert duration_ms == 1088500


def test_srt_duration_to_minutes():
    """Duration in minutes should be calculated from SRT, not word count."""
    captions = parse_srt(SAMPLE_SRT)
    duration_ms = get_srt_duration(captions)
    duration_minutes = round(duration_ms / 60000, 2)
    assert duration_minutes == pytest.approx(18.14, abs=0.01)


def test_get_srt_duration_empty():
    """Empty caption list returns 0."""
    assert get_srt_duration([]) == 0


def test_srt_file_uses_actual_duration_not_word_count():
    """Regression test: 18-minute episode got duration_minutes=33.52 from word count."""
    captions = parse_srt(SAMPLE_SRT)
    duration_ms = get_srt_duration(captions)
    duration_minutes = round(duration_ms / 60000, 2)
    assert duration_minutes > 1.0
    assert duration_minutes == pytest.approx(18.14, abs=0.01)


def test_calculate_transcript_metrics_excludes_srt_markup():
    """word_count must count spoken words, not SRT timecodes and index numbers (#404).

    The naive ``content.split()`` counted ~4 markup tokens per caption, inflating
    the operator-facing word count by the caption count (job 48: 3750 vs 2318).
    """
    from api.services.utils import calculate_transcript_metrics

    srt = "\n".join(
        [
            "1",
            "00:00:00,000 --> 00:00:03,000",
            "one two three",
            "",
            "2",
            "00:00:03,000 --> 00:00:06,000",
            "four five six",
            "",
        ]
    )

    metrics = calculate_transcript_metrics(srt, is_srt=True)

    assert metrics["word_count"] == 6, "should count only the six spoken words"


def test_calculate_transcript_metrics_plain_text_unchanged():
    """Plain text keeps the simple whitespace count (#404 regression guard)."""
    from api.services.utils import calculate_transcript_metrics

    metrics = calculate_transcript_metrics("one two three four five")

    assert metrics["word_count"] == 5
