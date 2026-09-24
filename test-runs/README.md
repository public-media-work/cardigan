# test-runs

Hand-run pipeline experiments kept for reference. Each run is a dated folder holding the
exact prompts the worker built, every model output, the deterministic gate results, and a
findings write-up. These are evidence for bug reports, not fixtures for the test suite.

| Run | What | Start here |
|---|---|---|
| `2026-09-24-6POL0213-manual-supervisor/` | Inside Wisconsin Politics 6POL0213 driven through all five phases by a supervisor agent against `origin/main` e142891 (prod's revision), with OpenRouter replays of the formatter on Sonnet 5. Found why prod jobs pause and what unblocks them. | `FINDINGS-6POL0213.md`, then `RUNLOG.md` |

## Job archives live in the private data repo

Full job exports (transcripts, phase outputs, review notes, Airtable ids) are **not** kept in
this public repo. They go to the private `public-media-work/internal-production-data` repo
under `cardigan/<date>-<cohort>/`, produced with `export_jobs.py` here:

```bash
python test-runs/export_jobs.py --out ../internal-production-data/cardigan/<date>-<cohort> [--status paused failed]
```

Each export is one folder per job (`job.json` = the full API row incl. per-phase
model/cost/tokens, `validation_result`, `error_message`; plus every artifact the outputs
endpoint serves) and an `index.csv` / `index.json` with the fields worth charting. Source
transcripts are not served by the API; `transcript_file` in the index names them.

First cohort: `cardigan/2026-09-24-all-jobs/` — all 48 jobs on `cardigan01` as of
2026-09-24 (15 completed · 4 failed · 29 paused), exported before the paused/failed rows were
deleted from the app. Mark's ruling: performance data only; the episodes already published.

**Do not commit `test-runs/archive/` here.** This repo is public.

## Layout of a run folder

- `FINDINGS-*.md` — defects with IDs, severity, fix location, hotfix vs v-next tier.
- `RUNLOG.md` — chronological log with verbatim gate output and subagent self-reports.
- `harness.py` — builds prod's prompts via the worker's own methods and runs prod's gates
  (`intake`, `prompts <phase>`, `merge`, `gate formatter|validator`). Needs the repo's
  Python deps; set `ESC=<subdir>` to work on a variant such as `esc` or `final`.
- `replay_formatter.py` — one OpenRouter call with `finish_reason` and usage capture; key via
  `get-secret.sh OPENROUTER_API_KEY`. `compare_404.py` — main vs PR #409 chunking decision.
- `<phase>/system.md`, `user.md`, `output.md` — exactly what was sent and returned.
  `formatter/chunk0`, `chunk1` (prod's chunked path), `formatter/esc` (Opus escalation),
  `formatter/replay/raw_*.json` (OpenRouter bodies), `*/final` (the outputs that passed).
- Inputs: the `.srt` and `sst_context.json` (the SST fields prod would have fetched).
