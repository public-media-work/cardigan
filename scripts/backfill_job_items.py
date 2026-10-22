#!/usr/bin/env python3
"""Backfill job_items for jobs that ran before the item model existed (#426).

TEMPORARY: delete this script together with
``api/services/legacy_item_adapter.py`` when the first generator lands.

Rebuilds items for historical jobs by re-reading their phase outputs through
the legacy adapter and (when the job has a Media ID or a pinned record)
re-fetching the SST record. Resolving a record by Media ID pins it to the job.

Safe to re-run: ``upsert_job_items`` keys on (job_id, key) and never
overwrites an editor's review state or hand edit.

Usage::

    python scripts/backfill_job_items.py --dry-run --job 23   # report only
    python scripts/backfill_job_items.py --job 23             # one job
    python scripts/backfill_job_items.py --all                # every completed job
    python scripts/backfill_job_items.py --all --no-airtable  # skip SST lookups
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
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
from api.services.item_sync import fetch_sst_context, read_outputs  # noqa: E402
from api.services.items import build_items, premiere_date_proposals  # noqa: E402
from api.services.legacy_item_adapter import extract_legacy_proposals  # noqa: E402


async def backfill_job(job_id: int, *, dry_run: bool, use_airtable: bool) -> dict:
    """Rebuild one job's items. Returns a summary dict."""
    job = await get_job(job_id)
    if job is None:
        return {"job_id": job_id, "error": "not found"}

    outputs = read_outputs(job.project_path)
    present = [name for name, text in outputs.items() if text]

    sst_context = await fetch_sst_context(job, pin=not dry_run) if use_airtable else None

    proposals = extract_legacy_proposals(
        phase_models={phase.name: phase.model for phase in job.phases if phase.model},
        **outputs,
    )
    proposals.update(premiere_date_proposals(sst_context))

    rows = build_items(
        job_id=job_id,
        proposals=proposals,
        sst_context=sst_context,
        content_type=job.content_type or "full",
    )

    if not dry_run:
        await upsert_job_items(job_id, rows)

    return {
        "job_id": job_id,
        "project": job.project_name,
        "media_id": job.media_id,
        "outputs_found": present,
        "sst_context": sst_context is not None,
        "items": len(rows),
        "by_generation": Counter(row["generation_state"] for row in rows),
        "by_origin": Counter(row["origin"] for row in rows if row["generation_state"] == "ready"),
        "with_basis": sum(1 for row in rows if row["basis_value"]),
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
        help="Skip SST lookups; items get no basis and current_state='unknown'",
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

            generation = ", ".join(f"{k}={v}" for k, v in sorted(summary["by_generation"].items()))
            origins = ", ".join(f"{k}={v}" for k, v in sorted(summary["by_origin"].items()))
            print(
                f"  job {summary['job_id']:>4} {str(summary['project'])[:28]:28} "
                f"media_id={str(summary['media_id']):20} "
                f"sst={'yes' if summary['sst_context'] else 'no ':3} "
                f"items={summary['items']:2} basis={summary['with_basis']:2}  {generation}  [{origins}]"
            )
            if not summary["outputs_found"]:
                print("        ^ no phase outputs on disk — items exist but carry no proposals")
    finally:
        await close_db()

    return 0


if __name__ == "__main__":
    os.environ.setdefault("DATABASE_PATH", "./dashboard.db")
    raise SystemExit(asyncio.run(main()))
