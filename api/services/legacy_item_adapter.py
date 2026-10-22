"""TEMPORARY: read today's phase outputs as item proposals (#426).

DELETE THIS MODULE — and ``scripts/backfill_job_items.py`` with it — when the
first generator lands (see "Define the context bundle and the generator
contract", #427). Nothing else should import it; ``item_sync`` is its only
caller, so deletion is a one-file change plus that call site.

It exists so the item model holds real data before any generator exists: a
completed job's ``seo_output.md`` and ``formatter_output.md`` are mapped onto
the new item shape. Every value here came from a model, so every proposal is
``inferred``. Per-item tokens and cost are left null — the phase that produced
them ran once for several items and its spend can't be split honestly.

The analyst's brainstorming material (themes, speakers, place names,
editorial angles) is NOT read: those are parts of the context bundle, not
items.
"""

from __future__ import annotations

from typing import Mapping, Optional

from api.models.item import Origin
from api.services.items import Proposal
from api.services.style_engine import extract_seo_fields

GENERATOR = "legacy_adapter"

# Phase output file -> the extract_legacy_proposals kwarg it feeds.
PHASE_OUTPUTS: dict[str, str] = {
    "seo_output.md": "seo_md",
    "formatter_output.md": "formatter_md",
}

SEO_KEYS: tuple[str, ...] = ("title", "short_description", "long_description")


def extract_legacy_proposals(
    *,
    seo_md: str = "",
    formatter_md: str = "",
    phase_models: Optional[Mapping[str, str]] = None,
) -> dict[str, Proposal]:
    """Map phase outputs onto proposals for the items that have an extractor.

    A missing phase output yields no proposal (the item ships as
    ``not_generated``). A present output whose section can't be found yields
    a proposal with no value, so the item reads as ``failed`` rather than
    silently absent.
    """
    models = phase_models or {}
    proposals: dict[str, Proposal] = {}

    if seo_md:
        fields = extract_seo_fields(seo_md)
        for key in SEO_KEYS:
            span = getattr(fields, key, None)
            proposals[key] = Proposal(
                value=span.value if span is not None else None,
                origin=Origin.inferred,
                generator=f"{GENERATOR}:seo",
                model=models.get("seo"),
            )

    if formatter_md:
        proposals["formatted_transcript"] = Proposal(
            value=formatter_md,
            origin=Origin.inferred,
            generator=f"{GENERATOR}:formatter",
            model=models.get("formatter"),
        )

    return proposals
