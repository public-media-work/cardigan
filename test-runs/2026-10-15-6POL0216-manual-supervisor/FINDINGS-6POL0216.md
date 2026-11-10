# Findings: 6POL0216 manual supervisor run (2026-10-15)

Inside Wisconsin Politics 6POL0216 ("What are the track records of Tom Tiffany and David Crowley?"), 18:44, four
panelists. Code under test: `origin/main` 980f945 + #409 + #421 + #422 (local branch `sim/6POL0216-stack`; suite
1892 passed). The method is 6POL0215's, with one change forced by this episode: **every Claude Code subagent that
read the full caption text after the analyst was stopped by a safety classifier**, so the formatter and timestamp
phases ran as direct OpenRouter calls on `anthropic/claude-sonnet-5.5`. See "Method" below and RUNLOG.md.

**Prod outcome on the stack:** the job completes (QA gate COMPLETED on both the stack and main), with formatter
`needs_review` and SEO `ready_for_review` carried in the artifacts. No pause, no escalation.

## New

| ID | Sev | Finding | Where |
|---|---|---|---|
| **LLM-2** | P1 (blocks a model upgrade) | `anthropic/claude-sonnet-5.5` on OpenRouter **rejects `reasoning: {"enabled": false}` with HTTP 400** ("Reasoning is mandatory for this endpoint and cannot be disabled"). #409 sends exactly that on every formatter call. If `phase_models.formatter` moves to 5.5, or QA escalation resolves to a 5.x model, every formatter call hard-fails. | `worker._reasoning_payload` (#409) |
| **LLM-3** | P1 (same trigger) | With reasoning on, Sonnet 5.5 spent **7,613 reasoning tokens** on the single-call formatter. Completion was 15,148 of max_tokens 16,384, leaving **1,236 tokens of headroom**. On forced chunk 0 it used 7,944 reasoning out of 13,066 completion tokens, which is over the simulated 6,000 cap. #409's output-budget gate (`tokens_per_word` 2.0 × safety 0.8) budgets visible output only. On a model whose reasoning can't be disabled, the budget math under-chunks and the #403 truncation returns. Fix with LLM-2: a per-model capability flag, and either an effort/`max_reasoning_tokens` control or reasoning headroom subtracted in the budget gate. | `chunking.split_transcript` budget, `_reasoning_payload` |
| **TS-5** | P2 | `_extract_boundary_candidates` uses `_SPEAKER_TRANSITION_RE.match(caption.text.strip())` on the **raw** SRT, so only captions that *start* with `>>` become candidates. **10 of 23 `>>` markers are mid-caption and are dropped**, including 14:32, ">> All right. We have to move to … the race for attorney General", the episode's largest topic change. The model said so: "That point is not an approved boundary, so the 'Attorney general rematch' chapter starts at the nearest permitted transition (16:07)". That leaves about 1.5 minutes of AG discussion under "David Crowley's record". Fix: run `split_interior_speaker_changes` (the formatter already does) before extracting, or `search` instead of `match`. This differs from #449 (no `>>` at all). | `style_engine/pre_stage.py:660` |
| **FMT-4** | P2 (chunked path) | Chunk-local generic labels collide after the merge. Chunk 0 wrote `Speaker 1`/`Speaker 2` for the 0:28 greetings, and chunk 1 restarted at `Speaker 1` for a different person at 12:55. The merged document has "Speaker 1" naming two people. Chunk 1 also lacked the address cues the single call used to name most of these turns (the single call left only 3 `Panel Member` turns; the chunk path left 6 `Speaker N` turns). Fix: number generic labels globally in the merge, or forbid numbered generics in non-first chunks. | `merge_formatter_chunks`, chunk wrapper |
| **AN-3** | P3 | The analyst invents a "Panel member (unattributed)" label and tells the formatter to use it. The formatter system prompt allows only Narrator/Host/Guest/Speaker 1. The single call followed the analyst (`Panel Member`), and chunk 1 followed the system prompt (`Speaker N`). The same job gets inconsistent labels depending on path. | analyst prompt vs formatter prompt |
| **GLOSS-2** | P2 | Recurring Wisconsin politics names are missing from the glossary. "Michaels" (×4, = Tim Michels, 2022 GOP nominee for governor) was left unresolved by the analyst, the formatter and SEO. "Pinnacle Hills" is almost certainly the **Penokee Hills** (the Gogebic Taconite mine Tiffany's 2013 bill enabled). Every phase flagged it and none could fix it. Kaul/Toney corrections *did* work from the glossary. Same class as GLOSS-1 (#456). | glossary |
| **TEST-1** | method | Claude Code subagents (Sonnet/Opus 5.5) can't reliably stand in for full-transcript phases. **6 of 6** were stopped by Anthropic's classifier: formatter on Sonnet ×2 and Opus ×1, forced chunk 0, timestamp, and a control run with the self-report request removed. **Every recorded category is `reasoning_extraction`**, the anti-distillation safeguard ("reverse engineering or duplicating model outputs"). It is *not* a content category, so this was not a block on the political subject matter. The likely trigger is the harness framing ("stand in for an LLM API call … behave exactly as the model receiving this call would" over a captured prompt), which is unverified and was deliberately not probed further. These completed: the Haiku 5.5 runs (analyst, validator), Sonnet SEO (no transcript body) and forced chunk 1. The identical prompts as plain OpenRouter calls (Vertex) all succeeded. The full investigation with request IDs is in `CLASSIFIER-BLOCK-REPORT.md` (private folder). | `/test-run` skill (#462) |
| **SEO-5** | P3 (latent, 5.x only) | The SEO output template asks for `**Reasoning:**` fields and a `[Reasoning]` scoring column. That is one of the patterns Anthropic lists as triggering `reasoning_extraction` refusals on 5.x/Fable models. It did not trigger today, and prod runs 4.6. Rename the fields to "Why" / "Notes" before any move to 5.x, and treat `stop_reason: "refusal"` (HTTP 200) as its own pause reason. | `.claude/agents/seo*`, worker refusal handling |

## Recurring (cross-checked, not re-filed)

- **STY-2 (#451) bites for real this time.** Anya van Wagtendonk is on air (four turns), but the formatter's "authoritative spellings" list and SEO's "keyword candidates verified present" list are `Shawn Johnson, Johnson, Zac Schultz, Schultz, Rich Kremer, Kremer`. She's missing, and so are Tiffany, Crowley, Kaul and Toney. The formatter got her name right anyway, from SST and the analyst.
- **SEO-3 (#454) at its worst.** `formatter_output[:2000]` is the header and the review-notes block, so **zero dialogue lines** reach SEO. SEO's self-report: "I could not check spoken wording against the transcript".
- **SEO-4 (#459).** SEO replaced the editor's title "What are the track records of Tom Tiffany and David Crowley? | Inside Wisconsin Politics" (88 with the suffix, 60 without), saying it "appears to exceed 80 characters". It kept the SST short description verbatim. Measured: title 73, short 88, long 327, all within limits. There are no counts in the deliverable.
- **STY-3 (#453 class).** "Governor Scott Walker" and "Governor Tommy Thompson" became "Gov." inside verbatim speech. The single call otherwise kept garbled captions as captioned and listed them in review notes, which is better FMT-2 behaviour than 6POL0215. It made one flagged word fix ("in the" → "than the") and reconstructed "PBS Wisconsin Education slash events" into the URL `pbswisconsin.org/events`, which needs checking.
- **VAL-3 (#453).** The validator says it "could not confirm the formatted dialogue against the source" because there is no SRT in its input. It noticed that 100.3% coverage "can suggest added text" but did not flag it.
- **PROMPT-1.** `{model name you are running as}` is still unfilled in the analyst and SEO prompts.
- **WC-1.** `job.word_count` 5,101 vs 3,557 dialogue words (5,101 − 3,557 = 1,544 = 386 × 4).
- **TS-2.** "Duration 18:44 (exact)" vs the last cue end 00:18:44,024 (cosmetic).

## What worked

- AN-1: the analyst's acts end at 18:44 = the SRT end (31 timecodes, max 1,124 s).
- #409: a single call on the stack, where main would split into 2 chunks [2,053, 1,504]. The single-call formatter wrote its header (Duration 00:18:44 correct), review notes and footer. Completeness 100.3%, no seam gap.
- #421: the merged chunk header took Duration 00:18:44 from the SRT, and no "section 1 of" leaked. The stray seam `---` (FMT-3c) remains.
- #422: Sonnet SEO stayed within every limit, and the social draft reached the prompts.
- The validator passed everything, with the same COMPLETED routing on the stack and on main.

## Method notes (for #462)

- Formatter and timestamp ran as single OpenRouter calls (`replay_formatter.py`, key via `get-secret.sh`). Their
  output was promoted to `<phase>/output.md`, and the raw bodies and telemetry are kept in `<phase>/replay/`. The
  differences from prod are the model (5.5, not 4.6) and reasoning (on, which 5.5 requires). There are no
  self-reports for these phases, so their review notes carry that load.
- A worktree-isolated session can't Edit or Write the main checkout's `OUTPUT/`. Bash with literal absolute paths
  works. Reported to the skill author and fixed on #462.
