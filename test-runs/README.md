# test-runs

Hand-run pipeline experiments kept for reference. Each run is a dated folder holding the
exact prompts the worker built, every model output, the deterministic gate results, and a
findings write-up. These are evidence for bug reports, not fixtures for the test suite.

| Run | What | Start here |
|---|---|---|
| `2026-09-24-6POL0213-manual-supervisor/` | Inside Wisconsin Politics 6POL0213 driven through all five phases by a supervisor agent against `origin/main` e142891 (prod's revision), with OpenRouter replays of the formatter on Sonnet 5. Found why prod jobs pause and what unblocks them. | `FINDINGS-6POL0213.md`, then `RUNLOG.md` |
| `2026-10-15-6POL0216-manual-supervisor/` | Inside Wisconsin Politics 6POL0216 against main 980f945 + #409/#421/#422. Every Claude Code subagent that read the full SRT was stopped by a safety classifier, so the formatter and timestamp ran on OpenRouter `claude-sonnet-5.5`. Found that 5.5 rejects #409's reasoning-off (HTTP 400), plus TS-5 and FMT-4. Only the write-up and scripts are public; the data is in `internal-production-data`. | `FINDINGS-6POL0216.md`, then `RUNLOG.md` |

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
