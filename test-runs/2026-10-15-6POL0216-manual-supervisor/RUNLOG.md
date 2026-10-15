# Run log: 6POL0216 manual supervisor run (2026-10-15)

Transcript: Inside Wisconsin Politics 6POL0216, `https://mmingest.pbswi.wisc.edu/IWP/6POL0216.srt` (Last-Modified
2026-10-15 19:30:36 GMT, 34,668 B, 386 captions, 18.73 min).

**Code under test, "stack":** the local, never-pushed branch `sim/6POL0216-stack` (bb8a6d1) = `origin/main` 980f945
(#412 and #413 already merged) + #409 + #421 + #422, exported with `git archive`. **Comparison tree, "main":**
`origin/main` 980f945.

**Where the data is:** `~/Developer/cardigan/OUTPUT/supervised-runs/2026-10-15-6POL0216-manual-supervisor/`
(gitignored), copied to `public-media-work/internal-production-data` →
`cardigan/2026-10-15-6POL0216-manual-supervisor/6POL0216-stack/` (private). Paths below are relative to that folder.

**Model calls (mixed, by necessity):**

| Phase | Ran as | Model | Notes |
|---|---|---|---|
| analyst | Claude Code subagent | Haiku | self-report |
| formatter | **OpenRouter** | `anthropic/claude-sonnet-5.5`, reasoning **on** | 3 subagent attempts blocked (see "Formatter") |
| formatter forced chunk 0 | **OpenRouter** | sonnet-5.5, reasoning on | subagent blocked |
| formatter forced chunk 1 | Claude Code subagent | Sonnet | self-report |
| seo | Claude Code subagent | Sonnet | self-report |
| validator | Claude Code subagent | Haiku | self-report |
| timestamp | **OpenRouter** | sonnet-5.5, reasoning on | subagent blocked |

Prod would use sonnet-4.6 with reasoning off for the formatter. The operator chose the newest Sonnet for the API
replays. 5.5 does not allow reasoning off (LLM-2).

## Stack assembly
- #409 conflicted with main in `api/services/worker.py` (`_chunk_wrapper_instructions`/`_retry_context_section` from #413
  vs `_reasoning_payload`) and `tests/api/test_worker.py`. Both are pure additions at the same anchor, so I kept both.
- #421 conflicted in `worker.py` (two add/add hunks: `_chunk_header_fields` vs `_reasoning_payload`/`_phase_attempt_count`/
  `_truncation_message`) and in `test_worker.py` (`TestTruncationMessage` and `TestChunkHeaderFields` interleaved
  around a shared `_worker()` helper; each class got its own copy). These are the same trivial conflicts as 6POL0215.
- #422 merged clean. Suite: **1892 passed, 1 skipped, 5 xfailed**.

## SST (read-only, Airtable MCP)
- SST `recWKkxdUYa0yiGTB`, Project `recm6gIpONx1u9Kxu`. Release Title "What are the track records of Tom Tiffany and
  David Crowley? | Inside Wisconsin Politics". Short and Long Description are populated. Host, Presenter and Keywords are empty.
  The social draft names all four reporters, and so does the host intro. The Project Notes and Description are byte-identical to
  the 6POL0215 pull (checked with `diff`).
- `_extract_speakers_from_sst` → host Shawn Johnson; presenter Zac Schultz, Anya van Wagtendonk, Rich Kremer. This
  time the series-level roster is correct for the episode.

## Intake (`intake-stack.txt`, `intake-main.txt`)
| | stack | main |
|---|---|---|
| `job.word_count` | 5,101 | 5,101 |
| dialogue words | 3,557 | 3,557 |
| captions / after `>>` split | 386 / 396 | 386 / 396 |
| formatter chunking | **single call** (budget 6,553 words at max_tokens 16,384) | **2 chunks** [2,053, 1,504] |
| SEO | sonnet-4.6 / openrouter | haiku-4.5 / openrouter-cheapskate |
| reasoning | `disabled_phases: [formatter]` | none |

WC-1 persists: 5,101 − 3,557 = 1,544 = 386 × 4.

## analyst (Haiku subagent)
- 2,034 words with all sections. **AN-1 gate PASS**: 31 timecodes, max 1,124 s = SRT 1,124.0 s.
- PROMPT-1: `{model name you are running as}` is unfilled. The subagent filled in its own identity.
- Flags (self-report and output): "Michaels" ×4 left unnamed (Tim Michels, not in the glossary), "Pinnacle Hills" flagged
  (Penokee Hills), the series-name conflict ("WPM News Politics Roundup" in Project Notes), and its own inferences
  ("January 2021", "1990s"). The system prompt also tells it to look up `knowledge/Media ID Prefixes.md`, which isn't in
  the call. The Kaul/Toney glossary corrections were applied.
- It invents a "Panel member (unattributed)" label and passes it to the formatter (AN-3).

## Formatter
### Prompt capture (stack)
- Single call, `reasoning: {"enabled": false}`, system 22,744 / user 56,325 chars, no unfilled placeholders.
- **STY-2 (#451):** "Proper nouns (authoritative spellings)" = `Shawn Johnson, Johnson, Zac Schultz, Schultz, Rich Kremer,
  Kremer`. On-air speaker Anya van Wagtendonk is missing.

### Subagent attempts: all blocked by the safety classifier (TEST-1)
| Attempt | Model | Result |
|---|---|---|
| single call | Sonnet | `stop_reason: refusal` on the first turn after reading the input (~17 s). No files. |
| forced chunk 0 (to 12:55) | Sonnet | stopped on the output turn. No files. |
| forced chunk 1 (12:55–end) | Sonnet | **completed** |
| single call, identical retry | Sonnet | blocked (~16 s) |
| single call, identical prompt | **Opus** | blocked (~96 s) |

The subagent transcripts (`~/.claude/projects/…/subagents/agent-*.jsonl`) show only the refusal stop and the platform
notice. The withheld content and the reason are not logged. The captions both runs had just read (~10:00–13:00) are
routine. Nothing was reworded or split to get past the stops. The retries were identical, with one escalation to Opus,
at the operator's direction. Write-up: `CLASSIFIER-BLOCK-REPORT.md`.

### OpenRouter replay (`formatter/replay/`)
- First call with prod's payload (`reasoning: {"enabled": false}`) on sonnet-5.5 → **HTTP 400 "Reasoning is mandatory
  for this endpoint and cannot be disabled." (LLM-2)** (`raw_154722.json`).
- Reasoning on: HTTP 200, `finish_reason: stop` (`end_turn`), provider Google, 85.7 s, $0.217. Prompt 32,741 tokens.
  Completion 15,148 = **reasoning 7,613** + content 7,535. **Headroom 1,236 tokens (LLM-3).**
- Promoted to `formatter/output.md`. Gates (`formatter/gate.txt`):
  - completeness **100.3%** (3,567 / 3,557), seam no gap (386 captions). No pause.
  - Header with Duration **00:18:44** (correct), REVIEW NOTES present, `**Status:** needs_review`.
  - Turns: Shawn Johnson 13, Zac Schultz 5, Anya van Wagtendonk 4, Rich Kremer 2, Panel Member 3. The SRT has 23 `>>`.
- Review notes: three unattributable short turns; Michaels unresolved; Pinnacle Hills; five garbled AG-segment
  captions **kept as captioned** and listed for checking (no FMT-2-style rewriting this time); one flagged fix
  ("in the" → "than the"); `pbswisconsin.org/events` reconstructed from "PBS Wisconsin Education slash events".
- **STY-3:** captions "Governor Scott Walker" and "Governor Tommy Thompson" → "Gov." in the transcript.

## Supplement: forced chunk path (`formatter/forced-chunk/`, `FORCE_MAX_TOKENS=6000`; not prod behaviour)
- chunk0 (OpenRouter sonnet-5.5): 74.3 s, completion 13,066 = **reasoning 7,944** + content 5,122. That is over the
  simulated 6,000 cap, so prod on this config would truncate (LLM-3). chunk1 (Sonnet subagent): completed.
- Merge with the real `merge_formatter_chunks` and `header_fields` from `_chunk_header_fields`: header Duration 00:18:44
  (#421 works), completeness 99.6%, no seam gap, no "section 1 of" leak.
- **FMT-3c persists:** a stray `---` at the seam (merged line 73).
- **FMT-4:** both chunks numbered their own `Speaker N`, so "Speaker 1" is a greeter at 0:28 *and* a different person at
  12:55. The chunk path left 6 `Speaker N` turns where the single call left 3. Chunk 1's self-report: "Numbering is
  local to this chunk and may not match chunk 1."
- AN-3: the single call used the analyst's `Panel Member` label, and chunk 1 used the system prompt's `Speaker N`.

## seo (Sonnet subagent; prod sonnet-4.6 per #422)
- **SEO-3 (#454) at its worst:** `formatter_output[:2000]` is the header plus the review notes, with zero dialogue lines.
  "Keyword candidates verified present" is the STY-2 speaker list.
- Measured (`gate seo`): title **73**, short **88** (the SST short description kept verbatim), long **327**. All within limits.
  There are no counts in the deliverable. Status `ready_for_review`.
- **SEO-4 (#459):** it replaced the editor's title (88 chars with the 28-char suffix, 60 without). Self-report: "If the title
  measures 80 or under, the SST title should be kept, since the instructions say to preserve editor intent."

## validator (Haiku subagent): **not blocked** (its input has the formatted transcript but no raw SRT)
- Bare JSON. analyst, formatter and seo pass, **overall pass**. `gate validator`: **QA gate COMPLETED** with
  CODE=stack *and* CODE=main.
- VAL-3 (#453): "Raw caption file / SRT. Without it I could not confirm the formatted dialogue against the source."
  It noticed 100.3% coverage "can suggest added text" but did not flag it. It accepted `ready_for_review` as legal.
  Its hand counts matched the measured lengths.

## timestamp (prod sonnet-4.6, runs after the validator)
- Pre-stage: Duration 18:44, chapter limit 7, **13 boundary candidates**.
- **TS-5:** `_extract_boundary_candidates` (`pre_stage.py:660`) matches `>>` only at caption start on the raw SRT. 10 of 23
  `>>` are mid-caption, including 14:32 ">> All right. We have to move to this other extremely important constitutional
  office … the race for attorney General".
- The subagent was blocked (~15 s). OpenRouter sonnet-5.5 replay (`timestamp/replay/`): 200/stop, 24.6 s, $0.073,
  reasoning 2,619 / content 592.
- Output: 0:00 Episode intro · 3:16 Tom Tiffany's record · 11:01 David Crowley's record · 16:07 Attorney general rematch ·
  17:38 Circumstances and governing style. Its notes: "The attorney general discussion begins around 14:34 … That point is
  not an approved boundary, so the 'Attorney general rematch' chapter starts at the nearest permitted transition
  (16:07)." That puts about 1.5 minutes of AG discussion under the Crowley chapter. The last chapter title is vague.
- TS-2: "18:44 (exact)" vs the last cue end 00:18:44,024.

## QA gate (stack): **COMPLETED.** No escalation, no pause.
