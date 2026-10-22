"""Refresh a job's items from its outputs and its SST record (#426).

The impure counterpart to ``api.services.items``: reads output files, resolves
and fetches the SST record, and upserts rows. Assembly stays pure and
testable over there.

The target record is resolved once per job. A job already pinned to a record
(``jobs.airtable_record_id``) uses it; otherwise the Media ID is looked up and
the result is pinned, so a Media ID edited in AirTable mid-review can't
redirect the job to another record. A job with neither has no record: items
are complete and copyable, with no basis.

Safe to call repeatedly. ``upsert_job_items`` keys on (job_id, key) and never
overwrites the editor's columns, so re-running after a phase retry updates
proposals without discarding a verdict or an edit.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from api.models.job import Job, JobUpdate
from api.services.database import get_job, update_job, upsert_job_items
from api.services.items import build_items, normalize_sst_record, premiere_date_proposals
from api.services.legacy_item_adapter import PHASE_OUTPUTS, extract_legacy_proposals

logger = logging.getLogger(__name__)


def read_outputs(project_path: str) -> dict[str, str]:
    """Read whatever phase outputs the legacy adapter needs. Missing files yield ''."""
    base = Path(project_path)
    outputs = {kwarg: "" for kwarg in PHASE_OUTPUTS.values()}

    for filename, kwarg in PHASE_OUTPUTS.items():
        path = base / filename
        try:
            if path.is_file():
                outputs[kwarg] = path.read_text(encoding="utf-8", errors="replace")
        except OSError as err:
            # A single unreadable output must not sink the whole refresh.
            logger.warning(
                "Could not read phase output",
                extra={"path": str(path), "error": str(err)},
            )

    return outputs


async def fetch_sst_context(job: Job, *, pin: bool = True) -> Optional[dict[str, Any]]:
    """Resolve, pin and fetch the job's SST record, or None if there is none.

    ``pin=False`` resolves without writing the record id back to the job.

    AirTable being unreachable degrades items to no basis; it must never fail
    a job or block extraction.
    """
    if not job.airtable_record_id and not job.media_id:
        return None

    try:
        from api.services.airtable import get_airtable_client

        client = get_airtable_client()
        record_id = job.airtable_record_id

        if not record_id:
            found = await client.search_sst_by_media_id(job.media_id)
            if found is None:
                return None
            record_id = found["id"]
            if pin:
                await update_job(job.id, JobUpdate(airtable_record_id=record_id))

        record = await client.get_sst_record(record_id, by_field_id=True)
        if record is None:
            return None

        return normalize_sst_record(record)

    except Exception as err:
        logger.warning(
            "Could not fetch SST context for items",
            extra={"job_id": job.id, "media_id": job.media_id, "error": str(err)},
        )
        return None


def copy_generation_enabled(llm_config: Optional[dict]) -> bool:
    """The instance setting ``routing.items.copy_generation`` (default on)."""
    items_cfg = ((llm_config or {}).get("routing") or {}).get("items") or {}
    return bool(items_cfg.get("copy_generation", True))


async def refresh_job_items(job_id: int, *, llm_config: Optional[dict] = None, use_airtable: bool = True) -> int:
    """Rebuild a job's items from its outputs and SST record.

    Returns the number of item rows written (0 if the job is missing).
    """
    job = await get_job(job_id)
    if job is None:
        logger.warning("Cannot refresh items for unknown job", extra={"job_id": job_id})
        return 0

    sst_context = await fetch_sst_context(job) if use_airtable else None
    phase_models = {phase.name: phase.model for phase in job.phases if phase.model}

    proposals = extract_legacy_proposals(phase_models=phase_models, **read_outputs(job.project_path))
    proposals.update(premiere_date_proposals(sst_context))

    rows = build_items(
        job_id=job_id,
        proposals=proposals,
        sst_context=sst_context,
        copy_generation=copy_generation_enabled(llm_config),
        content_type=job.content_type or "full",
    )

    written = await upsert_job_items(job_id, rows)

    logger.info(
        "Refreshed job items",
        extra={
            "job_id": job_id,
            "items_written": written,
            "sst_context": sst_context is not None,
        },
    )
    return written
