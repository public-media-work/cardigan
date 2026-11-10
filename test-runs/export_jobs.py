#!/usr/bin/env python3
"""Export Cardigan jobs from a running instance into an archive folder for later analysis.

For every job (or those matching --status) it saves:
  <out>/<id>-<media_id or project>/job.json        the full job row from GET /api/jobs/{id}
  <out>/<id>-.../<phase>_output.md, manifest.json  every artifact the outputs endpoint serves
and writes <out>/index.json + <out>/index.csv with the fields worth charting.

Read-only. Fetches via curl (some sandboxes allow curl to the LAN host but not Python sockets).
Source transcripts are NOT exported: the API does not serve them (outputs allowlist); the
index records transcript_file so they can be pulled from the box or mmingest later.

Usage:
  python test-runs/export_jobs.py --out ../internal-production-data/cardigan/2026-09-24-all-jobs
  python test-runs/export_jobs.py --status paused failed --out ...
  CARDIGAN_API_URL=https://cardigan.riechers.co (default)
  CARDIGAN_API_KEY=...  sent as X-API-Key when set
"""

import argparse
import csv
import json
import os
import subprocess
import sys
from pathlib import Path

BASE = os.environ.get("CARDIGAN_API_URL", "https://cardigan.riechers.co")


def get(url: str) -> tuple[int, str]:
    # Send X-API-Key when the deployment has auth on. The key goes in on stdin (-H @-) so it
    # never shows up in the process list.
    key = os.environ.get("CARDIGAN_API_KEY", "")
    header = f"X-API-Key: {key}" if key else ""
    r = subprocess.run(
        ["curl", "-s", "-m", "30", "-H", "@-", "-w", "\n%{http_code}", url],
        input=header,
        capture_output=True,
        text=True,
    )
    body, _, code = r.stdout.rpartition("\n")
    return int(code or 0), body


def list_jobs(statuses: list[str] | None) -> list[dict]:
    """Every job (optionally per status). The endpoint pages at 100 max and has no `limit`."""
    jobs = []
    for status in statuses or [None]:
        page = 1
        while True:
            url = f"{BASE}/api/queue/?page={page}&page_size=100&sort=oldest" + (f"&status={status}" if status else "")
            code, body = get(url)
            if code != 200:
                raise SystemExit(f"queue list failed: HTTP {code} ({url})")
            data = json.loads(body)
            jobs += data["jobs"]
            if page >= data["total_pages"]:
                break
            page += 1
    return jobs


def _first_line_mentions(path: Path, word: str) -> bool:
    if not path.exists():
        return False
    with path.open(encoding="utf-8") as fh:
        return word in fh.readline()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--status", nargs="*", default=None, help="only these statuses (default: all)")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    jobs = list_jobs(args.status)
    jobs.sort(key=lambda j: j["id"])

    index = []
    for j in jobs:
        jid = j["id"]
        code, body = get(f"{BASE}/api/jobs/{jid}")
        if code != 200:
            print(f"job {jid}: HTTP {code}, skipped", file=sys.stderr)
            continue
        d = json.loads(body)
        slug = (d.get("media_id") or d.get("project_name") or "job").replace("/", "_")
        jdir = out / f"{jid:03d}-{slug}"
        jdir.mkdir(exist_ok=True)
        (jdir / "job.json").write_text(json.dumps(d, indent=2, sort_keys=True), encoding="utf-8")

        # Try every allowlisted artifact name, not just what the manifest lists: paused and
        # failed jobs have no manifest (it is written on completion) yet still have phase files.
        files = [
            "manifest.json",
            "analyst_output.md",
            "formatter_output.md",
            "seo_output.md",
            "validator_output.md",
            "timestamp_output.md",
            "copy_editor_output.md",
        ] + [v for v in (d.get("outputs") or {}).values() if v]
        saved = []
        # Manifest names come from the server; keep only the basename so none can escape jdir.
        for fn in dict.fromkeys(Path(f).name for f in files):
            code, body = get(f"{BASE}/api/jobs/{jid}/outputs/{fn}")
            if code == 200:
                (jdir / fn).write_text(body, encoding="utf-8")
                saved.append(fn)

        phases = d.get("phases") or []
        if isinstance(phases, str):
            phases = json.loads(phases)
        vr = d.get("validation_result") or {}
        flags = []
        for ph, r in (vr.get("phase_results") or {}).items():
            for f in r.get("flags") or []:
                flags.append(f"{ph}: {f}")
        row = {
            "id": jid,
            "status": d.get("status"),
            "media_id": d.get("media_id"),
            "project_name": d.get("project_name"),
            "transcript_file": d.get("transcript_file"),
            "airtable_record_id": d.get("airtable_record_id"),
            "content_type": d.get("content_type"),
            "duration_minutes": d.get("duration_minutes"),
            "word_count": d.get("word_count"),
            "queued_at": d.get("queued_at"),
            "completed_at": d.get("completed_at"),
            "app_version": d.get("app_version"),
            "actual_cost": d.get("actual_cost"),
            "current_phase": d.get("current_phase"),
            "retry_count": d.get("retry_count"),
            "auto_escalated_at": d.get("auto_escalated_at"),
            "error_message": d.get("error_message"),
            "validator_overall": vr.get("overall"),
            "validator_flags": flags,
            "phases": [
                {
                    k: p.get(k)
                    for k in (
                        "name",
                        "status",
                        "model",
                        "cost",
                        "tokens",
                        "input_tokens",
                        "output_tokens",
                        "retry_count",
                    )
                }
                for p in phases
            ],
            "chunked": _first_line_mentions(jdir / "formatter_output.md", "chunked"),
            "artifacts_saved": saved,
        }
        index.append(row)
        print(f"job {jid:>3} {row['status']:<9} {slug:<20} artifacts={len(saved)}")

    (out / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    cols = [
        "id",
        "status",
        "media_id",
        "project_name",
        "transcript_file",
        "content_type",
        "duration_minutes",
        "word_count",
        "chunked",
        "queued_at",
        "completed_at",
        "app_version",
        "actual_cost",
        "current_phase",
        "retry_count",
        "auto_escalated_at",
        "validator_overall",
        "error_message",
    ]
    phase_names = ["analyst", "formatter", "seo", "validator", "timestamp"]
    with (out / "index.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            cols + [f"{p}_{k}" for p in phase_names for k in ("model", "cost", "output_tokens")] + ["validator_flags"]
        )
        for r in index:
            by = {p["name"]: p for p in r["phases"]}
            w.writerow(
                [r.get(c) for c in cols]
                + [by.get(p, {}).get(k) for p in phase_names for k in ("model", "cost", "output_tokens")]
                + [" | ".join(r["validator_flags"])]
            )
    print(f"\n{len(index)} jobs -> {out}  (index.json, index.csv)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
