#!/usr/bin/env python3
"""Submit a fetched YouTube transcript to Cardigan and collect the results.

Given a run directory produced by fetch_video.py (snippet.json + one .srt/.txt
transcript), this script:

1. POSTs the transcript to Cardigan's bulk upload endpoint
   (POST /api/upload/transcripts) — the same path file ingest uses, so the real
   4-phase worker runs and the Media ID in the filename auto-links the Airtable
   SST record.
2. Polls GET /api/jobs/{id} until the job reaches a terminal state, printing
   phase progress.
3. Pulls the pipeline outputs (seo_output.md, validator_output.md,
   analyst_output.md) and the SST snapshot (GET /api/jobs/{id}/sst-metadata)
   into the run directory — everything the copy-audit report needs alongside
   snippet.json.

Targets production (https://cardigan.riechers.co) by default per CLAUDE.md; honor
CARDIGAN_API_URL / CARDIGAN_API_KEY.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests

API_URL = os.environ.get("CARDIGAN_API_URL", "https://cardigan.riechers.co").rstrip("/")
API_KEY = os.environ.get("CARDIGAN_API_KEY")

TERMINAL_STATUSES = {"completed", "failed", "cancelled", "paused", "investigating"}
OUTPUT_FILES = ["seo_output.md", "validator_output.md", "analyst_output.md"]
POLL_SECONDS = 15
TIMEOUT_MINUTES = 45


def _headers() -> dict:
    return {"X-API-Key": API_KEY} if API_KEY else {}


def find_transcript(run_dir: Path) -> Path:
    candidates = sorted(
        p for p in run_dir.iterdir() if p.suffix in (".srt", ".txt") and not p.name.startswith("captions")
    )
    if not candidates:
        sys.exit(f"No transcript (.srt/.txt) found in {run_dir} — run fetch_video.py first.")
    if len(candidates) > 1:
        print(f"Multiple transcripts in {run_dir}; using {candidates[0].name}")
    return candidates[0]


def submit(transcript: Path) -> int:
    with transcript.open("rb") as fh:
        resp = requests.post(
            f"{API_URL}/api/upload/transcripts",
            files=[("files", (transcript.name, fh, "text/plain"))],
            headers=_headers(),
            timeout=60,
        )
    resp.raise_for_status()
    body = resp.json()
    status = body["files"][0]
    if not status["success"]:
        sys.exit(f"Upload rejected: {status.get('error')}")
    job_id = status["job_id"]
    print(f"Queued as job {job_id} ({API_URL}/api/jobs/{job_id})")
    return job_id


def wait(job_id: int) -> dict:
    deadline = time.monotonic() + TIMEOUT_MINUTES * 60
    last_line = ""
    while True:
        resp = requests.get(f"{API_URL}/api/jobs/{job_id}", headers=_headers(), timeout=30)
        resp.raise_for_status()
        job = resp.json()
        status = job["status"]
        phases = {p["name"]: p["status"] for p in job.get("phases", [])}
        line = f"status={status} phases={phases}"
        if line != last_line:
            print(line)
            last_line = line
        if status in TERMINAL_STATUSES:
            return job
        if time.monotonic() > deadline:
            sys.exit(f"Timed out after {TIMEOUT_MINUTES} minutes; job {job_id} is {status}.")
        time.sleep(POLL_SECONDS)


def collect(job_id: int, job: dict, run_dir: Path) -> None:
    for filename in OUTPUT_FILES:
        resp = requests.get(f"{API_URL}/api/jobs/{job_id}/outputs/{filename}", headers=_headers(), timeout=30)
        if resp.status_code == 200:
            (run_dir / filename).write_bytes(resp.content)
            print(f"Saved {filename}")
        else:
            print(f"No {filename} (HTTP {resp.status_code})")

    resp = requests.get(f"{API_URL}/api/jobs/{job_id}/sst-metadata", headers=_headers(), timeout=30)
    if resp.status_code == 200:
        (run_dir / "sst_metadata.json").write_text(json.dumps(resp.json(), indent=2), encoding="utf-8")
        print("Saved sst_metadata.json")
    else:
        print(
            f"No SST metadata (HTTP {resp.status_code}) — the job likely has no "
            "linked Airtable record. Log the Media-ID miss in FINDINGS.md."
        )

    (run_dir / "job.json").write_text(json.dumps(job, indent=2), encoding="utf-8")
    print("Saved job.json")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.exit("usage: submit_and_wait.py <run_dir>   (e.g. runs/<videoId>)")
    run_dir = Path(argv[1])
    if not run_dir.is_dir():
        sys.exit(f"{run_dir} is not a directory.")

    transcript = find_transcript(run_dir)
    print(f"Submitting {transcript.name} to {API_URL}")
    job_id = submit(transcript)
    job = wait(job_id)

    if job["status"] != "completed":
        print(f"Job {job_id} ended {job['status']} — collecting whatever exists anyway.")
    collect(job_id, job, run_dir)

    print(
        f"\nRun dir complete: {run_dir}\n"
        "Next: fill templates/COPY_AUDIT_TEMPLATE.md using snippet.json + "
        "sst_metadata.json + seo_output.md."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
