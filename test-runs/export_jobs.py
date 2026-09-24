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
  python test-runs/export_jobs.py --out test-runs/archive/2026-09-24-all-jobs
  python test-runs/export_jobs.py --status paused failed --out ...
  CARDIGAN_API_URL=http://cardigan01:8100 (default)
"""
import argparse
import csv
import json
import os
import subprocess
import sys
from pathlib import Path

BASE = os.environ.get("CARDIGAN_API_URL", "http://cardigan01:8100")


def get(url: str) -> tuple[int, str]:
    r = subprocess.run(["curl", "-s", "-m", "30", "-w", "\n%{http_code}", url], capture_output=True, text=True)
    body, _, code = r.stdout.rpartition("\n")
    return int(code or 0), body


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--status", nargs="*", default=None, help="only these statuses (default: all)")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    code, body = get(f"{BASE}/api/queue/?limit=500")
    if code != 200:
        print(f"queue list failed: HTTP {code}", file=sys.stderr)
        return 1
    jobs = json.loads(body)
    jobs = jobs if isinstance(jobs, list) else jobs.get("jobs") or jobs.get("items")
    if args.status:
        jobs = [j for j in jobs if j.get("status") in set(args.status)]
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
        for fn in dict.fromkeys(files):
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
                {k: p.get(k) for k in ("name", "status", "model", "cost", "tokens", "input_tokens", "output_tokens", "retry_count")}
                for p in phases
            ],
            "chunked": any("chunked" in (open(jdir / f, encoding="utf-8").readline() if (jdir / f).exists() else "") for f in ["formatter_output.md"]),
            "artifacts_saved": saved,
        }
        index.append(row)
        print(f"job {jid:>3} {row['status']:<9} {slug:<20} artifacts={len(saved)}")

    (out / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    cols = [
        "id", "status", "media_id", "project_name", "transcript_file", "content_type", "duration_minutes",
        "word_count", "chunked", "queued_at", "completed_at", "app_version", "actual_cost", "current_phase",
        "retry_count", "auto_escalated_at", "validator_overall", "error_message",
    ]
    phase_names = ["analyst", "formatter", "seo", "validator", "timestamp"]
    with (out / "index.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols + [f"{p}_{k}" for p in phase_names for k in ("model", "cost", "output_tokens")] + ["validator_flags"])
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
