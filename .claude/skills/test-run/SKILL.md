---
name: test-run
description: Use when asked to run a Cardigan job locally with coding agents instead of prod. Triggers include a supervised run, a simulated run, a test run, "run the agent instructions locally", generating metadata from a caption/SRT file without deploying, or checking whether open fix PRs would let a job complete. Also use when gathering evidence for pipeline bugs from a real transcript.
---

# Supervised test run

Drive one caption file through Cardigan's real prompt builders and gates, with each model call made by a local subagent at prod's tier. You come out with every artifact prod would produce, a chronological log, and a findings write-up.

**The method's one rule:** never re-implement what prod does. Prompts are *captured* from the worker's own `_run_phase`, gates are the worker's own gate code, and SST context comes from the worker's own `_fetch_sst_context`. `test-runs/harness.py` does all of this.

**Interpreter:** always use `~/Developer/cardigan/venv/bin/python test-runs/harness.py …` (the main checkout's venv has the deps; a worktree has none). Per-command flags: `… harness.py <command> -h`.

## Before starting: settle four things with the operator

Ask with `AskUserQuestion`. If you're a subagent without it, stop and return the open questions to your caller.

1. **Code under test.** Use plain `main`, or `main` plus specific open fix PRs (`gh pr list --state open`). Plain `main` is always exported too, as the comparison tree.
2. **Model calls.** Default to subagents only. Make OpenRouter calls only if asked; they cost money, and the key comes from `get-secret.sh OPENROUTER_API_KEY`.
3. **Media ID.** Use it if known (it drives the SST lookup). Archival files often have none.
4. **Write-up destination.** The run data always goes private. Ask whether findings also become a public docs PR, issues, or both.

## Names and places

- **label**: the media ID, or the caption file's stem (`6POL0215`, `Ojibwe_Music`).
- **cohort**: `~/Developer/cardigan/OUTPUT/supervised-runs/<YYYY-MM-DD>-<label>-manual-supervisor/`. This path is in the **main checkout**, even when you work from a worktree. `OUTPUT/` is gitignored, so the operator can find it. Use literal absolute paths.
- **unit**: one run directory inside the cohort. Use `<label>-stack` for the code under test and `<label>-main` for the comparison. The harness names nothing itself; `--run-dir` is always explicit.
- **Code trees**: `<cohort>/_code-stack`, `<cohort>/_code-main`. Never archived.
- **Log and findings**: `<cohort>/RUNLOG.md`, `<cohort>/FINDINGS-<label>.md`.
- **Archive**: private repo `public-media-work/internal-production-data` → `cardigan/<cohort name>/`.

`public-media-work/cardigan` is **PUBLIC**. Transcripts, captured prompts, outputs, self-reports, SST JSON and producers' drafts go to the private repo only. A public PR carries at most the write-up and harness changes, and only when the operator says so.

Use one command per Bash call, with literal paths. Worktree guards refuse compound or variable-built commands.

## Steps

1. **Code.** `harness.py code --dest <cohort>/_code-main`. If there are PRs, also run `harness.py code --dest <cohort>/_code-stack --prs 409,421`. With no PRs, the stack *is* main, so use `_code-main` for both units and skip the second unit's phases.
   - A PR stack is a detached git **worktree**; plain main is a plain export.
   - A merge conflict stops the command. If it's trivial (both sides add code at the same anchor), resolve it in that worktree, `git commit`, then re-run the same `code --dest … --prs <remaining PRs>`. It continues in the existing worktree. If it isn't trivial, ask. Log every conflict.
   - Then run `~/Developer/cardigan/venv/bin/python -m pytest -q` with cwd set to the stack tree.
2. **Setup** each unit: `harness.py setup --run-dir <cohort>/<label>-stack --srt <url|path> --code <cohort>/_code-stack [--media-id ID]`. Do the same for `<label>-main` with `--code <cohort>/_code-main`. `setup` reports `ascii_only` and the count of `?`-only captions, which are music glyphs lost upstream (#457). `source_last_modified` is only known for URLs.
3. **SST.** `harness.py sst-fields` prints the field names. Read the SST record (table `tblTKFOwTvK7xw1H5`, search `Media ID`) and its linked Project (`tblU9LfZeVNicdB5e`), **read-only**. Save each as `{"id": …, "fields": {<field name>: value}}`, then run `harness.py sst --run-dir <unit> --record r.json --project p.json` for each unit.
   - If there's no record, run `sst --run-dir <unit>` with no files. No `sst_context.json` is written, and intake then shows `sst_loaded: false`. Running without SST is a finding (#452).
   - Never write to Airtable.
4. **Intake** each unit with `harness.py intake --run-dir <unit>`. Record the legend items below in RUNLOG.
5. **Phases, strictly in prod's order:** analyst → formatter → seo → validator → timestamp. The validator *phase* runs before timestamp, and the validator never sees timestamp output. For each phase, on the stack unit:
   - `harness.py prompts --run-dir <unit> <phase>`. Note the style-block lines, `unfilled` placeholders and `kwargs` (reasoning control). `model=None` means prod's default model.
   - `harness.py brief --run-dir <unit> <phase> [--chunk N]`. Send the text between the rules as one subagent prompt, verbatim, with the Agent tool's `model` set to the tier it names. Chunks are independent, so dispatch them in parallel, then run `harness.py merge --run-dir <unit>`.
   - `harness.py gate --run-dir <unit> <phase>`. Paste the output into RUNLOG.
   - Read `selfreport.md`. Most findings come from these: instruction conflicts, guesses, words the model changed.
6. **QA gate, after timestamp.** Run `harness.py gate --run-dir <unit> validator` on the stack unit. Copy the stack's phase outputs into the main unit and gate it too, to see whether main's routing differs. The result is COMPLETED, PROD WOULD PAUSE, or PROD WOULD ESCALATE.
   - On escalate: recapture the flagged phases with `--variant esc --model <next tier>` and **no `--flags`**, because prod's escalation carries none (#450). Then dispatch, recapture and dispatch the validator with `--variant esc`, and gate `--variant esc`.
7. **Supervisor interventions come only after the prod-faithful result is recorded.** When a deliverable is unusable (for example, one timestamp chapter on a 28-minute piece), copy the captured prompt into `<phase>/supervised/`, make the **smallest** edit, dispatch, and label it `SUP-n` in RUNLOG with the exact change. That edit *is* the proposed fix.
8. **Supplementary experiments** (forced chunking with `--force-max-tokens 6000 --variant forced-chunk`, main's validator prompt over the stack's outputs, …) are labelled as not-prod in RUNLOG. Variant gates never overwrite the prod-path `completeness.json`.

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
2. Copy the cohort (without `_code-*`) into the private repo. Add `index.csv` and `index.json` (`harness.py index` gives one row per unit) and a row in `cardigan/README.md`. Grep for secrets, then commit and push.
3. Ask before filing issues, commenting on PRs, or opening a public docs PR.

## Caveats to state in every write-up

- Subagent wall time is about 10× an API call.
- Subagents have tools prod's model lacks (one counted characters with Python), so don't read timing or counting accuracy as prod behaviour.
- With no OpenRouter calls there is no `finish_reason` or reasoning-token data.
