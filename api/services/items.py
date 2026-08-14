"""Item extraction and derivation (issue #329).

Turns a job's phase outputs into :class:`~api.models.item.Item` rows — the
editor-facing primitive that replaces agent-shaped ``*_output.md`` documents.

Pure functions only: no DB access, no HTTP. The caller supplies the phase
markdown and (optionally) the SST context dict; persistence lives in
``api.services.database.upsert_job_items``.

Three sources of proposed values:

``llm``
    Extracted from phase markdown. Only fields with a reliable extractor are
    populated — see ``ITEM_SPECS``. The five drifting SEO fields ship as
    ``awaiting_source``; parsing prose that isn't a contract yields
    confidently-wrong values, and the fix is structured phase output, not a
    better regex.
``deterministic``
    Computed by code. The four "entered into system X" dates all derive from
    ``Digital Premiere``; the link fields await integrations Cardigan lacks.
``human``
    Set when an editor edits a value in place. Never produced here.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping, Optional

from api.models.item import (
    CurrentState,
    ItemCategory,
    ItemLayer,
    ItemSource,
    ItemStatus,
)
from api.services.style_engine import extract_seo_fields
from api.services.style_engine.rules import load_rules

logger = logging.getLogger(__name__)

# Values longer than this are omitted from list responses unless the caller
# passes ?include=values. formatted_transcript is routinely 50-150KB.
LARGE_VALUE_CHARS = 4000

# ---------------------------------------------------------------------------
# Single Source Status (BETA) — fld26q5kEJicSYHX8
#
# The only review signal the SST exposes. Observed 2026-08-13 across the live
# base: 6POL n=40 -> 20 producer-approved, 20 empty. Whole table n=3000 ->
# 2291 empty, 664 producer, 18 promotions, 12 ready-for-review, 12 digital,
# 3 producer-issue. 661 of the 664 producer-approved records carry a Short
# Description, so the signal tracks "a human blessed real content" closely.
#
# It is a RECORD-level signal, not field-level: it says the record was
# approved, not that this particular field was read.
# ---------------------------------------------------------------------------
SST_STATUS_FIELD = "Single Source Status (BETA)"

LADDER_REVIEWED = frozenset(
    {
        "Edited & Approved by Digital",
        "Edited & Approved by Promotions",
        "Final Copy & Images Approved by Producer",
    }
)
LADDER_FLAGGED = "Producer Issue - See Promo Notes"


@dataclass(frozen=True)
class ItemSpec:
    """Static declaration of one item key."""

    key: str
    layer: ItemLayer
    category: ItemCategory
    source: ItemSource
    phase: Optional[str] = None
    airtable_field: Optional[str] = None
    airtable_field_id: Optional[str] = None
    sst_key: Optional[str] = None  # key into the sst_context dict
    limit_key: Optional[str] = None  # key into house_style limits.fields
    blocked_on: Optional[str] = None  # non-None => ships as awaiting_source

    @property
    def is_blocked(self) -> bool:
        return self.blocked_on is not None


# ---------------------------------------------------------------------------
# The registry. Order here is the order the approval surface renders.
# ---------------------------------------------------------------------------
ITEM_SPECS: tuple[ItemSpec, ...] = (
    # -- context: never published -------------------------------------------
    ItemSpec("context.themes", ItemLayer.context, ItemCategory.context, ItemSource.llm, phase="analyst"),
    ItemSpec("context.speakers", ItemLayer.context, ItemCategory.context, ItemSource.llm, phase="analyst"),
    ItemSpec("context.place_names", ItemLayer.context, ItemCategory.context, ItemSource.llm, phase="analyst"),
    ItemSpec("context.editorial_angles", ItemLayer.context, ItemCategory.context, ItemSource.llm, phase="analyst"),
    # -- deliverables with a real extractor ---------------------------------
    ItemSpec(
        "title",
        ItemLayer.deliverable,
        ItemCategory.copy,
        ItemSource.llm,
        phase="seo",
        airtable_field="Release Title",
        airtable_field_id="fldXqxjjxR4z5IJv6",
        sst_key="title",
        limit_key="title",
    ),
    ItemSpec(
        "short_description",
        ItemLayer.deliverable,
        ItemCategory.copy,
        ItemSource.llm,
        phase="seo",
        airtable_field="Short Description",
        airtable_field_id="fldDwTtKlOCdgKHpW",
        sst_key="short_description",
        limit_key="short_description",
    ),
    ItemSpec(
        "long_description",
        ItemLayer.deliverable,
        ItemCategory.copy,
        ItemSource.llm,
        phase="seo",
        airtable_field="Long Description",
        airtable_field_id="fld6HsWiKL77bFqo1",
        sst_key="long_description",
        limit_key="long_description",
    ),
    ItemSpec(
        "formatted_transcript",
        ItemLayer.deliverable,
        ItemCategory.transcript,
        ItemSource.llm,
        phase="formatter",
        # No SST field exists for the transcript. Delivery is Drive/download
        # (#259), so this item is approvable but has no publish destination.
    ),
    # -- deliverables awaiting structured phase output ----------------------
    # These are generated entirely from the role prompt today and the reports
    # drift; their sections do not align 1:1 with Airtable fields. Parsing
    # them would produce confidently-wrong values rather than missing ones.
    # Unblocked by the hybrid pipeline (#295), not by a better parser.
    ItemSpec(
        "social_description",
        ItemLayer.deliverable,
        ItemCategory.copy,
        ItemSource.llm,
        phase="seo",
        airtable_field="Social Media Description",
        airtable_field_id="fldntHlzk6PfIT5k2",
        sst_key="social_description",
        limit_key="social_description",
        blocked_on="structured_output:seo",
    ),
    ItemSpec(
        "social_tags",
        ItemLayer.deliverable,
        ItemCategory.copy,
        ItemSource.llm,
        phase="seo",
        airtable_field="Social Media Tags",
        airtable_field_id="fldcenwfu4nEWjPbt",
        sst_key="social_tags",
        limit_key="social_tags",
        blocked_on="structured_output:seo",
    ),
    ItemSpec(
        "facebook_description",
        ItemLayer.deliverable,
        ItemCategory.copy,
        ItemSource.llm,
        phase="seo",
        airtable_field="Facebook Description",
        airtable_field_id="fldnprt2bJEsndv96",
        sst_key="facebook_description",
        limit_key="facebook_description",
        blocked_on="structured_output:seo",
    ),
    ItemSpec(
        "hashtags",
        ItemLayer.deliverable,
        ItemCategory.copy,
        ItemSource.llm,
        phase="seo",
        airtable_field="Hashtags",
        airtable_field_id="fldYSGo5EBidQYL7W",
        sst_key="hashtags",
        limit_key="hashtags",
        blocked_on="structured_output:seo",
    ),
    ItemSpec(
        "keywords",
        ItemLayer.deliverable,
        ItemCategory.metadata,
        ItemSource.llm,
        phase="seo",
        airtable_field="General Keywords/Tags",
        airtable_field_id="fldjdPEXZyvx3rc6Y",
        sst_key="keywords",
        limit_key="keywords",
        blocked_on="structured_output:seo",
    ),
    # -- deterministic dates: populators ship -------------------------------
    # All four derive from Digital Premiere. Verified equal in 4/4 sampled
    # 6POL records (Media Manager == Entered into YouTube == Digital Premiere).
    #
    # Known tension: docs/media-manager-api-access-request.md describes the
    # "Media Manager" field as a last-modified stamp rather than a publish
    # flag, and audit-assets reads it as a publish proxy. We treat these as
    # INTENT dates ("when this ideally entered the system") per the #329
    # grilling, and never overwrite a date a human already set.
    ItemSpec(
        "media_manager_date",
        ItemLayer.deliverable,
        ItemCategory.metadata,
        ItemSource.deterministic,
        airtable_field="Media Manager",
        airtable_field_id="fldYDsAyzPfpvYy41",
        sst_key="media_manager_date",
    ),
    ItemSpec(
        "entered_youtube_date",
        ItemLayer.deliverable,
        ItemCategory.metadata,
        ItemSource.deterministic,
        airtable_field="Entered into YouTube",
        airtable_field_id="fldlza0yrvJlZZL35",
        sst_key="entered_youtube_date",
    ),
    ItemSpec(
        "protrack_date",
        ItemLayer.deliverable,
        ItemCategory.metadata,
        ItemSource.deterministic,
        airtable_field="ProTrack",
        airtable_field_id="fldOYf9FmJY92v3ML",
        sst_key="protrack_date",
    ),
    ItemSpec(
        "transcript_date",
        ItemLayer.deliverable,
        ItemCategory.metadata,
        ItemSource.deterministic,
        airtable_field="Transcript",
        airtable_field_id="fldmWY1YglRIpbF2E",
        sst_key="transcript_date",
    ),
    # -- deterministic links: awaiting integrations -------------------------
    # NOTE: imdb_link (fldPHQv2Gi4OMe6cA) is deliberately absent. It is a
    # url-typed field humans use as a scratchpad — live values include
    # "pending" and "done, pending". Writing it destroys tracking notes.
    ItemSpec(
        "youtube_link",
        ItemLayer.deliverable,
        ItemCategory.metadata,
        ItemSource.deterministic,
        airtable_field="YouTube Link",
        airtable_field_id="fldwV4AKLOqH77End",
        sst_key="youtube_link",
        blocked_on="integration:youtube_api",
    ),
    ItemSpec(
        "final_website_link",
        ItemLayer.deliverable,
        ItemCategory.metadata,
        ItemSource.deterministic,
        airtable_field="Final Website Link",
        airtable_field_id="flddW2yRjRcIY6g8U",
        sst_key="final_website_link",
        blocked_on="integration:portal",
    ),
    ItemSpec(
        "media_manager_iframe",
        ItemLayer.deliverable,
        ItemCategory.metadata,
        ItemSource.deterministic,
        airtable_field="Media Manager iFrame",
        airtable_field_id="flduc22XbwzMwlCyt",
        sst_key="media_manager_iframe",
        blocked_on="integration:mm_api",
    ),
    ItemSpec(
        "eidr_id",
        ItemLayer.deliverable,
        ItemCategory.metadata,
        ItemSource.deterministic,
        airtable_field="EIDR ID",
        airtable_field_id="fldcMsVsRnP7Bk5BS",
        sst_key="eidr_id",
        blocked_on="integration:eidr",
    ),
)

SPECS_BY_KEY: dict[str, ItemSpec] = {spec.key: spec for spec in ITEM_SPECS}

DETERMINISTIC_DATE_KEYS: tuple[str, ...] = (
    "media_manager_date",
    "entered_youtube_date",
    "protrack_date",
    "transcript_date",
)


# ---------------------------------------------------------------------------
# Markdown section extraction (context layer)
# ---------------------------------------------------------------------------


def _section(md: str, heading: str) -> Optional[str]:
    """Return the body under a ``## <heading>`` up to the next ``##``.

    Heading match is case-insensitive and matches a *prefix* of the actual
    heading, so "SEO Keywords" finds "## SEO Keywords (Preliminary)".
    Returns None when the section is absent; never raises.
    """
    if not md:
        return None
    pattern = rf"^##\s+{re.escape(heading)}[^\n]*\n(.*?)(?=^##\s|\Z)"
    match = re.search(pattern, md, re.MULTILINE | re.DOTALL | re.IGNORECASE)
    if not match:
        return None
    body = match.group(1).strip()
    return body or None


def _labelled_block(section: str, label: str) -> Optional[str]:
    """Return the lines under a ``**Label:**`` marker up to the next one.

    Used for the ``**Location-Specific:**`` block nested inside the analyst's
    "SEO Keywords (Preliminary)" section.
    """
    if not section:
        return None
    pattern = rf"\*\*{re.escape(label)}:?\*\*\s*\n?(.*?)(?=\n\s*\*\*[A-Z][^*]*:?\*\*|\Z)"
    match = re.search(pattern, section, re.DOTALL | re.IGNORECASE)
    if not match:
        return None
    body = match.group(1).strip()
    return body or None


def extract_context_items(analyst_md: str) -> dict[str, Optional[str]]:
    """Pull the four context items out of the analyst's brainstorming doc.

    Headings come from ``prompts/analyst.md``'s documented structure. Missing
    sections yield None rather than raising — a partial analyst output should
    still produce the items it can.
    """
    seo_keywords = _section(analyst_md, "SEO Keywords")
    return {
        "context.themes": _section(analyst_md, "Key Themes"),
        "context.speakers": _section(analyst_md, "Speakers"),
        "context.place_names": (_labelled_block(seo_keywords, "Location-Specific") if seo_keywords else None),
        "context.editorial_angles": _section(analyst_md, "Editorial Opportunities"),
    }


def extract_deliverable_items(seo_md: str = "", formatter_md: str = "") -> dict[str, Optional[str]]:
    """Pull the deliverables that have a reliable extractor.

    Only title / short_description / long_description come from ``seo_md``;
    those are the three the style engine already governs via
    ``extract_seo_fields``. The transcript is taken whole.
    """
    values: dict[str, Optional[str]] = {}

    if seo_md:
        fields = extract_seo_fields(seo_md)
        for key in ("title", "short_description", "long_description"):
            span = getattr(fields, key, None)
            values[key] = span.value if span is not None else None

    if formatter_md:
        values["formatted_transcript"] = formatter_md

    return values


# ---------------------------------------------------------------------------
# Current-value derivation
# ---------------------------------------------------------------------------


def normalize_sst_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Map a raw Airtable SST record onto the ``sst_context`` shape.

    Driven by ``ITEM_SPECS`` so the field mapping can't drift from the
    registry: every spec carrying both an ``airtable_field`` and an
    ``sst_key`` contributes one entry. Also carries the review ladder and
    ``Digital Premiere``, which the deterministic date populators need.

    Accepts a record as returned by
    ``AirtableClient.search_sst_by_media_id`` — ``{"id", "fields", ...}``.
    """
    fields = record.get("fields", {}) or {}

    context: dict[str, Any] = {
        spec.sst_key: fields.get(spec.airtable_field) for spec in ITEM_SPECS if spec.sst_key and spec.airtable_field
    }
    context[SST_STATUS_FIELD] = fields.get(SST_STATUS_FIELD)
    context["digital_premiere"] = fields.get("Digital Premiere")
    context["record_id"] = record.get("id")
    context["media_id"] = fields.get("Media ID")
    return context


def derive_current_state(
    current_value: Any,
    ladder_value: Optional[str],
    *,
    fetched: bool,
) -> CurrentState:
    """Derive an item's ``current_state`` from the field and the record ladder.

    Precedence (per the #329 resolution):

    1. never fetched            -> ``unknown``
    2. record is Producer Issue -> ``flagged`` (regardless of field value)
    3. field is blank           -> ``empty``   (fill freely — first author)
    4. record approved by a human -> ``reviewed``
    5. otherwise                -> ``unreviewed``

    ``empty`` deliberately wins over the ladder for a blank field: Cardigan is
    the first author regardless of whether the record was approved, which is
    the "fill gaps vs. refine what's there" split.
    """
    if not fetched:
        return CurrentState.unknown

    if ladder_value == LADDER_FLAGGED:
        return CurrentState.flagged

    if current_value is None or not str(current_value).strip():
        return CurrentState.empty

    if ladder_value in LADDER_REVIEWED:
        return CurrentState.reviewed

    return CurrentState.unreviewed


def _premiere_date(sst_context: Mapping[str, Any]) -> Optional[date]:
    """Parse ``Digital Premiere`` into a date. None when absent or malformed."""
    raw = sst_context.get("digital_premiere")
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    # Airtable returns ISO-8601, commonly "2026-05-28T22:00:00.000Z".
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        logger.debug("Could not parse Digital Premiere value %r", text)
        return None


def _char_limit(limit_key: Optional[str], limits: Mapping[str, Any]) -> Optional[int]:
    """Look up a field's max length from house_style limits, if it has one."""
    if not limit_key:
        return None
    entry = limits.get(limit_key) or {}
    maximum = entry.get("max")
    return maximum if isinstance(maximum, int) else None


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def build_items(
    *,
    job_id: int,
    analyst_md: str = "",
    seo_md: str = "",
    formatter_md: str = "",
    sst_context: Optional[Mapping[str, Any]] = None,
    phase_models: Optional[Mapping[str, str]] = None,
    content_type: str = "full",
) -> list[dict[str, Any]]:
    """Build every item row for a job, ready for ``upsert_job_items``.

    Args:
        job_id: owning job.
        analyst_md / seo_md / formatter_md: phase outputs. Absent phases
            simply yield items with no proposed value.
        sst_context: normalized Airtable record (see
            ``api.services.airtable._extract_sst_fields``). ``None`` means the
            record was never fetched — every item lands in ``unknown``, which
            is the state for 27 of 30 production jobs today (#331).
        phase_models: phase name -> model id, for provenance.
        content_type: 'full' | 'short' | 'clip', selects house-style limit
            overrides.

    Returns a list of column dicts. Pure — no DB, no network.
    """
    fetched = sst_context is not None
    sst: Mapping[str, Any] = sst_context or {}
    models = phase_models or {}
    ladder = sst.get(SST_STATUS_FIELD)

    try:
        limits = load_rules().limits_for(content_type=content_type)
    except Exception:  # pragma: no cover - config problems must not break extraction
        logger.warning("Could not load house-style limits; items will carry no char_limit", exc_info=True)
        limits = {}

    proposed: dict[str, Optional[str]] = {}
    proposed.update(extract_context_items(analyst_md))
    proposed.update(extract_deliverable_items(seo_md=seo_md, formatter_md=formatter_md))

    premiere = _premiere_date(sst)

    rows: list[dict[str, Any]] = []
    for spec in ITEM_SPECS:
        current_value = sst.get(spec.sst_key) if spec.sst_key else None
        current_state = derive_current_state(current_value, ladder, fetched=fetched)

        proposed_value = proposed.get(spec.key)
        status = ItemStatus.pending_review
        source_blocked_on = None

        if spec.is_blocked:
            # Nothing can produce a value yet — integration missing, or the
            # phase's output isn't a parseable contract.
            status = ItemStatus.awaiting_source
            source_blocked_on = spec.blocked_on
            proposed_value = None

        elif spec.key in DETERMINISTIC_DATE_KEYS:
            if premiere is None:
                status = ItemStatus.awaiting_source
                source_blocked_on = "field:digital_premiere"
            else:
                proposed_value = premiere.isoformat()
                # Fill-only. A date a human already set is never overwritten
                # silently; it goes to the editor instead.
                status = ItemStatus.approved if current_state == CurrentState.empty else ItemStatus.pending_review

        rows.append(
            {
                "job_id": job_id,
                "key": spec.key,
                "layer": spec.layer.value,
                "category": spec.category.value,
                "source": spec.source.value,
                "proposed_value": proposed_value,
                "current_value": (str(current_value) if current_value is not None else None),
                "current_state": current_state.value,
                "current_fetched_at": datetime.utcnow() if fetched else None,
                "status": status.value,
                "source_blocked_on": source_blocked_on,
                "flags": None,
                "phase": spec.phase,
                "model": models.get(spec.phase) if spec.phase else None,
                "char_limit": _char_limit(spec.limit_key, limits),
                "airtable_field": spec.airtable_field,
                "airtable_field_id": spec.airtable_field_id,
            }
        )

    return rows
