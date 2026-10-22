---
name: test-run
description: Use when asked to run a Cardigan job locally with coding agents instead of prod. Triggers include a supervised run, a simulated run, a test run, "run the agent instructions locally", generating metadata from a caption/SRT file without deploying, or checking whether open fix PRs would let a job complete. Also covers a full local worker run (real API + worker against a throwaway DB) when bug-testing a deployment or worker change, and gathering evidence for pipeline bugs from a real transcript.
---

# Supervised test run

Drive one caption file through Cardigan's real prompt builders and gates, with each model call made by a local subagent at prod's tier. You come out with every artifact prod would produce, a chronological log, and a findings write-up.

**The method's one rule:** never re-implement what prod does. Prompts are *captured* from the worker's own `_run_phase`, gates are the worker's own gate code, and SST context comes from the worker's own `_fetch_sst_context`. `test-runs/harness.py` does all of this.

**Interpreter:** run the harness with the Python of the cardigan checkout's venv, `<main checkout>/venv/bin/python test-runs/harness.py …`. On Mark's machine that is `~/Developer/cardigan/venv/bin/python`. A worktree has no venv of its own, so use the main checkout's. If there is no venv, create one per CLAUDE.md first. Per-command flags: `… harness.py <command> -h`.

## Two modes

| Mode | Use it for | Model calls | You get |
|---|---|---|---|
| **Supervised** (the default; steps below) | Instruction conflicts, guesses, what a fix PR changes, phase by phase | Subagents at prod's tier, OpenRouter replay as fallback | Every artifact plus a self-report per phase |
| **Full worker** ("Full local worker run" below) | The deployed code path end to end: queue, DB, real routing, retries, pauses, the QA gate, items, cost and token telemetry. Use it when the operator is testing a deployment or a worker/routing change. | Prod's own `llm.chat` to OpenRouter (paid) | `run.db`, the job row, and outputs as prod would write them. No self-reports. |

If the request doesn't say which, use supervised. Use full worker when the operator says "full run", "like prod", or the thing under test is the worker, the queue or routing, not a prompt.

## Before starting: use the defaults and announce them

Don't open with a questionnaire. State the defaults in one short message and start. The operator redirects if they want something else. Ask (`AskUserQuestion`) only when the request contradicts a default or leaves something it needs unsettled. If you're a subagent without that tool, return the open question to your caller.

| Setting | Default | Change it when |
|---|---|---|
| **Code under test** | Latest `origin/main`, no PRs. `code` fetches before exporting. | The operator names PRs to stack. If they want PRs **landed** first, land them before step 1, so the code under test is main again. |
| **Backend** | Subagent per phase. After one identical retry of a classifier stop, fall back to OpenRouter `replay --newest` for that phase. On 6POL0216 (Oct 2026, sonnet-5.5, 19 min) that cost $0.22 for the formatter and $0.07 for timestamp. The default covers it. | The operator says no paid calls (then stop and ask at the first stop) or OpenRouter for everything. |
| **Media ID** | The caption file's stem when it looks like one (`6POL0217.srt` → `6POL0217`). `sst --live` confirms it; no record means none. | Archival or ad-hoc files (`Ojibwe_Music.srt`) have no media ID. |
| **Write-up** | Private only: the cohort goes to the private repo. Ask before filing issues, commenting on PRs or opening a public docs PR. | The operator asks for issues or a docs PR up front. |

## Names and places

- **label**: the media ID, or the caption file's stem (`6POL0215`, `Ojibwe_Music`).
- **cohort**: `<checkout>/OUTPUT/supervised-runs/<YYYY-MM-DD>-<label>-manual-supervisor/`, where `<checkout>` is **the checkout you are running in**. `OUTPUT/` is gitignored everywhere.
  - In the main checkout, that is `~/Developer/cardigan/OUTPUT/…`, which is where the operator looks.
  - In a **worktree** session, use the worktree's own `OUTPUT/`. A worktree-isolated session can't Edit/Write the main checkout, a `cd` there locks Bash, and subagents can't write there either. At close, copy the finished cohort to `~/Developer/cardigan/OUTPUT/supervised-runs/` with one `cp -R` using literal absolute paths, so the operator finds it in the usual place.
- **unit**: one run directory inside the cohort. Use `<label>-stack` for the code under test and `<label>-main` for the comparison. The harness names nothing itself; `--run-dir` is always explicit.
- **Code trees**: `<cohort>/_code-stack`, `<cohort>/_code-main`. Never archived.
- **Log and findings**: `<cohort>/RUNLOG.md`, `<cohort>/FINDINGS-<label>.md`.
- **Archive**: private repo `public-media-work/internal-production-data` → `cardigan/<cohort name>/`. Find the local clone with `find ~/Developer -maxdepth 3 -type d -name internal-production-data`. In Oct 2026 it was `~/Developer/pbswi-tools/internal-production-data`, and Mark may move or rename it, so don't hard-code the path and don't recreate `pbswi-tools/`. If there's no clone, ask where to put one (`gh repo clone public-media-work/internal-production-data <path>`). `git pull` before adding a cohort.

`public-media-work/cardigan` is **PUBLIC**. Transcripts, captured prompts, outputs, self-reports, SST JSON and producers' drafts go to the private repo only. A public PR carries at most the write-up and harness changes, and only when the operator says so.

Use one command per Bash call, with literal absolute paths: no shell variables, no `$(…)` or `<(…)`, no `cd` outside your checkout. Worktree guards refuse compound or variable-built commands. They also refuse a `python - <<EOF` heredoc whose text mentions git. Write helper scripts to the scratchpad and run them with `python3 -I <script> <args>`.

## Steps

1. **Code.** `harness.py code --dest <cohort>/_code-main`. If there are PRs, also run `harness.py code --dest <cohort>/_code-stack --prs 409,421`. With no PRs, the stack *is* main, so use `_code-main` for both units and skip the second unit's phases.
   - A PR stack is a detached git **worktree**; plain main is a plain export.
   - A merge conflict stops the command. If it's trivial (both sides add code at the same anchor), resolve it in that worktree, `git commit`, then re-run the same `code --dest … --prs <remaining PRs>`. It continues in the existing worktree. If it isn't trivial, ask. Log every conflict.
   - Then run `<main checkout>/venv/bin/python -m pytest -q` with cwd set to the stack tree.
2. **Setup** each unit: `harness.py setup --run-dir <cohort>/<label>-stack --srt <url|path> --code <cohort>/_code-stack [--media-id ID]`. Do the same for `<label>-main` with `--code <cohort>/_code-main`. `setup` reports `ascii_only` and the count of `?`-only captions, which are music glyphs lost upstream (#457). `source_last_modified` is only known for URLs.
3. **SST.** Run `harness.py sst --live --run-dir <unit>` for each unit. It runs the code under test's own `_fetch_sst_context` with its real Airtable client, narrowed to the three reads that function makes (key via `get-secret.sh AIRTABLE_API_KEY`). It writes `sst_context.json`, plus the raw `sst_record.json` and `sst_project.json` for the log. A failed read exits non-zero and doesn't masquerade as "no record".
   - **"SST context: None"** means no record has that media ID. Prod would run without SST, which is a finding (#452). Intake then shows `sst_loaded: false`.
   - **Manual fallback** (no key, or Airtable down): `harness.py sst-fields` prints the field names. Read the SST record (table `tblTKFOwTvK7xw1H5`, filter `{Media ID}`) and its linked Project (`tblU9LfZeVNicdB5e`) read-only, using the `/airtable-primer` REST recipes. This workspace has no Airtable MCP server. Save each as `{"id": …, "fields": {<field name>: value}}`, then run `sst --run-dir <unit> --record r.json --project p.json`. With no media ID, run `sst --run-dir <unit>` with no files.
   - Never write to Airtable.
4. **Intake** each unit with `harness.py intake --run-dir <unit>`. Record the legend items below in RUNLOG.
5. **Phases, strictly in prod's order:** analyst → formatter → seo → validator → timestamp. The validator *phase* runs before timestamp, and the validator never sees timestamp output. For each phase, on the stack unit:
   - `harness.py prompts --run-dir <unit> <phase>`. Note the style-block lines, `unfilled` placeholders and `kwargs` (reasoning control). `model=None` means prod's default model.
   - `harness.py brief --run-dir <unit> <phase> [--chunk N]`. Send the text between the rules as one subagent prompt, verbatim, with the Agent tool's `model` set to the tier it names. Chunks are independent, so dispatch them in parallel, then run `harness.py merge --run-dir <unit>`.
   - `harness.py gate --run-dir <unit> <phase>`. Paste the output into RUNLOG.
   - Read `selfreport.md`. Most findings come from these: instruction conflicts, guesses, words the model changed.
   - Don't edit the brief's wording. It frames the dispatch as the task itself, and the self-report asks for decisions and evidence, not reasoning, following Anthropic's guidance on reasoning-extraction refusals. An earlier brief asked the subagent to "act as one LLM call" and to explain how it resolved everything. That is the likely (unconfirmed) trigger of the 6POL0216 stops.
   - Unlike step 7's ban, this was a design change: it removed a request for reasoning rather than disguising one, it was approved by the operator, and it was not used to re-run anything blocked.
6. **QA gate, after timestamp.** Run `harness.py gate --run-dir <unit> validator` on the stack unit. Copy the stack's phase outputs into the main unit and gate it too, to see whether main's routing differs. The result is COMPLETED, PROD WOULD PAUSE, or PROD WOULD ESCALATE.
   - On escalate: recapture the flagged phases with `--variant esc --model <next tier>` and **no `--flags`**, because prod's escalation carries none (#450). Then dispatch, recapture and dispatch the validator with `--variant esc`, and gate `--variant esc`.
7. **When a subagent can't run a phase** (a safety-classifier stop, `stop_reason: refusal`, or a platform notice):
   - Expect this on Sonnet/Opus 5.x subagents writing a long full-transcript result (formatter, timestamp). On 6POL0216 every such run stopped with `stop_details.category: "reasoning_extraction"`, Anthropic's anti-distillation safeguard. Haiku and OpenRouter calls were not affected.
   - The UI notice hides the category. Find it in the subagent's JSONL (`~/.claude/projects/<slug>/<session>/subagents/agent-*.jsonl`) by searching for `stop_details`, and record the category and the request ID verbatim in RUNLOG, along with the transcript path.
   - At most **one identical** retry. If it stops again, fall back to OpenRouter for that phase under the default backend. If the operator ruled out paid calls, ask instead. Don't escalate a subagent to Opus to get past a stop: on 6POL0216, Opus 5.5 was stopped the same way after 96 s.
   - On 6POL0216, every Sonnet-tier formatter and timestamp run that held most of the transcript was stopped; only the last-third chunk got through (before the round-3 brief rewording, which has not yet faced a long transcript). Dispatch once plus its identical retry, then go straight to `replay` rather than spending more time. Update this line with what the next run shows.
   - **Never reword, summarise, split or re-chunk the input or the brief to get past a stop.** That is evading the classifier, not retrying, and it would also no longer test prod's prompt. If the subagent path needs to work for these phases, the route is Anthropic support with the request IDs.
   - Fallback: `harness.py replay --run-dir <unit> <phase> [--chunk N] [--newest]` sends the captured prompt unchanged. `--newest` picks the family's newest model through prod's roster. It sends the captured `max_tokens` and a reasoning control **only if the code under test passed one**: plain main sends none, and #409 adds `{enabled: false}` for the formatter. `notes` says when none was captured. Read `replay/telemetry.json`: `finish_reason` (`length` means truncated), `reasoning_tokens` and `headroom_tokens`, plus `notes`. Some newer models reject a disabled-reasoning control with a 400. `replay` then resends with reasoning on and records that in `notes`, which is a finding.
     - Mark wants `--newest`, the family's newest model, for replays unless he says otherwise. In Oct 2026 that was `anthropic/claude-sonnet-5.5`, which **requires** reasoning on (#465), so expect the 400-and-resend on every formatter replay from a stack that disables reasoning.
     - Reasoning tokens count against `max_tokens`. On 6POL0216 the formatter spent 7.6k of 16k on reasoning and had 1.2k headroom. Always record `headroom_tokens`. A small headroom is a finding even when `finish_reason` is `stop`.
   - Then `harness.py promote --run-dir <unit> <phase> [--chunk N]` makes `replay/output.md` the phase output for gates and later phases, and writes `backend.json`. It refuses unless `finish_reason` is `stop`. `--force` overrides that; record it in RUNLOG as a truncated output.
   - An OpenRouter phase has no self-report. Label the run **mixed-backend** in RUNLOG and FINDINGS, with the backend and model of every phase.
8. **Supervisor interventions come only after the prod-faithful result is recorded.** When a deliverable is unusable (for example, one timestamp chapter on a 28-minute piece), copy the captured prompt into `<phase>/supervised/`, make the **smallest** edit, dispatch, and label it `SUP-n` in RUNLOG with the exact change. That edit *is* the proposed fix.
9. **Supplementary experiments** (forced chunking with `--force-max-tokens 6000 --variant forced-chunk`, main's validator prompt over the stack's outputs, …) are labelled as not-prod in RUNLOG. Variant gates never overwrite the prod-path `completeness.json`.

### Intake legend

| Field | Meaning |
|---|---|
| `job_word_count` vs `dialogue_words` | Prod's `job.word_count` counts caption numbers and timecodes. `dialogue_words` is what completeness and chunking use (#404). |
| `turn_markers_>>`, `plain_name_labels`, `bracket_or_paren_cues` | Caption convention. With no `>>`, the timestamp phase gets no boundary candidates (#449). |
| `captions_after_>>_split` | Captions after the #269 interior-`>>` split that the formatter receives. |
| `chunking` | `single call`, or chunk word counts. |
| `timestamp_runs` | Whether prod runs the optional timestamp phase (long content). |
| `sst_loaded` | Whether `sst_context.json` exists. |

Known, unfiled-as-new: `{model name you are running as}` is left unfilled because `_load_agent_prompt` gets no model. This is PROMPT-1 from #410; record it, don't re-file.

## Full local worker run

This mode runs the real API and worker against a throwaway database, so the job goes through prod's whole path. First used on 2WLIGrizzWorksSM, 2026-10-21. It makes paid OpenRouter calls through prod's routing. Say so when you announce the defaults. Read the cost from the job row's `actual_cost` and report it.

1. **Code.** `harness.py code --dest <cohort>/_code-main`, with `--prs` for a stack, as in step 1 above. The tree has no venv, so use the main checkout's.
2. **Unit.** Make `<cohort>/<label>-full/transcripts/` and copy or download the SRT into it.
   - If the job already exists on prod, its exact transcript is on the host: `scp cardigan01:<transcripts dir>/archive/<file>.srt`. The prod path changes with the deploy (Docker volume vs. native state dir), so look it up, don't assume it.
   - The API serves only outputs, not the source file.
3. **API.** Use `run_in_background`, all paths absolute, and no variables:
   `DATABASE_PATH=<unit>/run.db OUTPUT_DIR=<unit>/OUTPUT TRANSCRIPTS_DIR=<unit>/transcripts <main checkout>/venv/bin/uvicorn api.main:app --port 8199 --app-dir <code tree>`
   - Port 8199 keeps it clear of a dev server on 8100.
   - `init_db` creates the schema, so no alembic step is needed.
   - Keys come from the app's own `api.services.secrets`: Docker secret, then env, then a Keychain fallback that prints a deprecation notice on Mark's Mac. That is how the 2026-10-21 run got them. If the API or worker can't resolve a key, flag it to the operator. Don't paste or hard-code a key.
   - The ingest and mmingest schedulers start too. They only index mmingest into the throwaway DB and never queue jobs.
4. **Worker.** Also in the background, with the same three env vars and the code tree as cwd: `<main checkout>/venv/bin/python run_worker.py --poll-interval 5 --concurrent 1`.
5. **Queue.** `curl -s -X POST http://localhost:8199/api/queue/ -H 'Content-Type: application/json' -d '{"project_name":"<label>","transcript_file":"<file>.srt"}'`. The worker resolves the media ID from the file name, and SST is read live, read-only.
6. **Watch.** Poll `GET /api/jobs/<id>` until `status` is `completed`, `paused` or `failed`. Use a Monitor whose filter matches all three; silence is not success. Record per-phase `model`, `output_tokens`, `cost` and `retry_count`, plus `error_message`, `validation_result` and `auto_escalated_at`. Then measure the outputs in `<unit>/OUTPUT/<label>/` with the same `gate` checks: point a supervised unit at them, or run the `phase_io` extractors directly.
7. **Stop** both background processes when done. `run.db`, `OUTPUT/` and the logs go to the private cohort like any other run data.

## What to check that no gate checks

- **Measure, never trust counts.** `gate seo` measures lengths. The models' own claims are often wrong.
- **Grep every SEO and description fact against the SRT.** SEO sees only about 2 KB of transcript (#454).
- **Program and names.** Was the Program guessed (#452)? Does the proper-noun line drop or invent names (#451)?
- **Fidelity.** Check the formatter's self-reported word changes against the captions, especially in quoted ads and clips (#453).
- **Attribution.** Where the formatter overrode the analyst, check the source and say who was right.

## Logging and findings

Write RUNLOG **as you go**, never reconstructed afterwards. Record verbatim gate output, decisions, and the self-report points that matter.

FINDINGS sections:
1. Bottom line: the prod outcome, and what reached the deliverable wrong.
2. Defect table: `ID | Sev (P0/P1/P2) | Tier (hotfix/v-next) | Where (file:line) | What | Fix`.
3. Regression table against open issues: `gh issue list` first, and reuse existing numbers.
4. Things that went right.
5. Recommendation.

If the operator wants the metadata itself, give it in chat with corrections noted, measured, and grounded.

## Closing the run

1. Delete each unit's `_scratch_out/` if it is present.
   - For a PR stack, run `git worktree remove <cohort>/_code-stack` after confirming in plain text.
   - For a plain export, delete the directory.
2. If you ran in a worktree, copy the cohort (without `_code-*`) to `~/Developer/cardigan/OUTPUT/supervised-runs/`.
3. Copy the cohort (without `_code-*`) into the private repo. Add `index.csv` and `index.json` (`harness.py index` gives one row per unit) and a row in `cardigan/README.md`. Grep for secrets, then commit and push.
4. Ask before filing issues, commenting on PRs, or opening a public docs PR.

## Caveats to state in every write-up

- Subagent wall time is about 10× an API call.
- Subagents have tools prod's model lacks (one counted characters with Python), so don't read timing or counting accuracy as prod behaviour.
- Subagent phases have no `finish_reason` or reasoning-token data; only `replay` phases do. Say which phases ran on which backend.
- Full-worker runs are prod's real path, with telemetry, but on a local box. Pause and heartbeat timing, the host's memory ceiling and the deployed config (env, secrets, routing overrides in the prod DB) can all differ from the deployment.
