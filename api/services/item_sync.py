"""Refresh a job's items from its phase outputs and the Airtable SST (#329).

The impure counterpart to ``api.services.items``: reads output files, fetches
the SST record, and upserts rows. Extraction logic itself stays pure and
testable over there.

Safe to call repeatedly. ``upsert_job_items`` keys on (job_id, key) and
preserves any verdict the editor already gave, so re-running after a phase
retry updates proposals without discarding approvals.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from api.models.job import Job
from api.services.database import get_job, upsert_job_items
from api.services.items import build_items, normalize_sst_record

logger = logging.getLogger(__name__)

# Phase output file -> the build_items kwarg it feeds.
_PHASE_OUTPUTS: dict[str, str] = {
    "analyst_output.md": "analyst_md",
    "seo_output.md": "seo_md",
    "formatter_output.md": "formatter_md",
}


def _read_outputs(project_path: str) -> dict[str, str]:
    """Read whatever phase outputs exist. Missing files yield ''."""
    base = Path(project_path)
    outputs = {kwarg: "" for kwarg in _PHASE_OUTPUTS.values()}

    for filename, kwarg in _PHASE_OUTPUTS.items():
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


async def _fetch_sst_context(job: Job) -> Optional[dict[str, Any]]:
    """Fetch and normalize the job's SST record, or None if unavailable.

    None is the honest answer for most production jobs today: ``media_id`` is
    null on 27 of 30, so there is nothing to look the record up by (#331).
    Every item then lands in ``current_state='unknown'`` rather than
    pretending the field was empty.
    """
    if not job.airtable_record_id and not job.media_id:
        return None

    try:
        from api.services.airtable import get_airtable_client

        client = get_airtable_client()
        record = None

        if job.airtable_record_id:
            record = await client.get_sst_record(job.airtable_record_id)
        if record is None and job.media_id:
            record = await client.search_sst_by_media_id(job.media_id)

        if record is None:
            return None

        return normalize_sst_record(record)

    except Exception as err:
        # Airtable being unreachable degrades items to 'unknown'; it must
        # never fail a job or block extraction.
        logger.warning(
            "Could not fetch SST context for items",
            extra={"job_id": job.id, "media_id": job.media_id, "error": str(err)},
        )
        return None


async def refresh_job_items(job_id: int) -> int:
    """Rebuild a job's items from its outputs and SST record.

    Returns the number of item rows written (0 if the job is missing).
    """
    job = await get_job(job_id)
    if job is None:
        logger.warning("Cannot refresh items for unknown job", extra={"job_id": job_id})
        return 0

    outputs = _read_outputs(job.project_path)
    sst_context = await _fetch_sst_context(job)
    phase_models = {phase.name: phase.model for phase in job.phases if phase.model}

    rows = build_items(
        job_id=job_id,
        sst_context=sst_context,
        phase_models=phase_models,
        content_type=job.content_type or "full",
        **outputs,
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
