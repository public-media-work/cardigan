#!/usr/bin/env python3
"""Backfill job_items for jobs that ran before the item model existed (#329).

Items are normally extracted when a job completes. This script rebuilds them
for historical jobs by re-reading their phase outputs from disk and (when the
job has an Airtable link) re-fetching the SST record.

Safe to re-run: ``upsert_job_items`` keys on (job_id, key) and preserves any
verdict an editor already gave.

Usage::

    python scripts/backfill_job_items.py --dry-run          # report only
    python scripts/backfill_job_items.py --job 23           # one job
    python scripts/backfill_job_items.py --all              # every completed job
    python scripts/backfill_job_items.py --all --no-airtable  # skip SST lookups
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.models.job import JobStatus  # noqa: E402
from api.services.database import (  # noqa: E402
    close_db,
    get_job,
    init_db,
    list_jobs,
    upsert_job_items,
)
from api.services.item_sync import _fetch_sst_context, _read_outputs  # noqa: E402
from api.services.items import build_items  # noqa: E402


async def backfill_job(job_id: int, *, dry_run: bool, use_airtable: bool) -> dict:
    """Rebuild one job's items. Returns a summary dict."""
    job = await get_job(job_id)
    if job is None:
        return {"job_id": job_id, "error": "not found"}

    outputs = _read_outputs(job.project_path)
    present = [name for name, text in outputs.items() if text]

    sst_context = await _fetch_sst_context(job) if use_airtable else None

    rows = build_items(
        job_id=job_id,
        sst_context=sst_context,
        phase_models={phase.name: phase.model for phase in job.phases if phase.model},
        content_type=job.content_type or "full",
        **outputs,
    )

    populated = sum(1 for row in rows if row["proposed_value"])
    by_status: dict[str, int] = {}
    for row in rows:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1

    if not dry_run:
        await upsert_job_items(job_id, rows)

    return {
        "job_id": job_id,
        "project": job.project_name,
        "media_id": job.media_id,
        "outputs_found": present,
        "sst_context": sst_context is not None,
        "items": len(rows),
        "populated": populated,
        "by_status": by_status,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--job", type=int, help="Backfill a single job by id")
    target.add_argument("--all", action="store_true", help="Backfill every completed job")
    parser.add_argument("--dry-run", action="store_true", help="Report without writing")
    parser.add_argument(
        "--no-airtable",
        action="store_true",
        help="Skip SST lookups; every item lands in current_state='unknown'",
    )
    args = parser.parse_args()

    await init_db()
    try:
        if args.job:
            job_ids = [args.job]
        else:
            jobs = await list_jobs(status=JobStatus.completed, limit=100)
            job_ids = [job.id for job in jobs]

        print(f"{'DRY RUN — ' if args.dry_run else ''}backfilling {len(job_ids)} job(s)\n")

        for job_id in job_ids:
            summary = await backfill_job(job_id, dry_run=args.dry_run, use_airtable=not args.no_airtable)
            if "error" in summary:
                print(f"  job {job_id}: {summary['error']}")
                continue

            statuses = ", ".join(f"{k}={v}" for k, v in sorted(summary["by_status"].items()))
            print(
                f"  job {summary['job_id']:>4} {str(summary['project'])[:28]:28} "
                f"media_id={str(summary['media_id']):20} "
                f"sst={'yes' if summary['sst_context'] else 'no ':3} "
                f"items={summary['items']:2} populated={summary['populated']:2}  {statuses}"
            )
            if not summary["outputs_found"]:
                print("        ^ no phase outputs on disk — items exist but carry no proposals")
    finally:
        await close_db()

    return 0


if __name__ == "__main__":
    os.environ.setdefault("DATABASE_PATH", "./dashboard.db")
    raise SystemExit(asyncio.run(main()))
