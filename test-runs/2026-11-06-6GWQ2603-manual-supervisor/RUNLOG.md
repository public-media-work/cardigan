# RUNLOG — 6GWQ2603 supervised run (2026-11-06)

Operator request: local test job on `~/Downloads/6GWQ2603.srt`; prod estimate ~$1.
Operator initially wanted to skip the formatter; after noting SEO/validator/timestamp all
consume formatter output, operator chose **run it via subagent** (all phases, prod-faithful).

## Setup
- Code under test: `origin/main` @ `262bdc7`, no PRs. Single unit `6GWQ2603-main`.
- pytest on export: 2091 passed, 1 skipped, 5 xfailed.
- setup: bytes=94084 ascii_only=True captions_that_are_only_'?'=0
- SST (live, read-only): record found — "The Stock I Come From | Great Wisconsin Quilt Show",
  program "The Great Wisconsin Quilt Show | 2026".

## Intake
- job_word_count 13124 vs dialogue_words 8420 (#404 gap).
- duration 68.67 min, 1176 captions, `>>` markers **0**, plain name labels 1, bracket/paren cues 39
  → no `>>` means timestamp gets no boundary candidates (#449).
- content_type full; timestamp_runs true; formatter_max_tokens 32768; chunking **single call**.
- phase models: analyst/validator `~anthropic/claude-haiku-latest`; formatter/seo/timestamp `~anthropic/claude-sonnet-latest`.
- reasoning disabled for formatter; reserve 8000.
- sst_loaded true.

## Phases

### analyst — subagent (haiku), 193 s
prompts: backend=openrouter-cheapskate model=None system=10986 user=96512 unfilled=['{model name you are running as}'] (PROMPT-1, #410)
gate:
```
duration 4120.1s; timecodes 82; max 4120s
AN-1 timings beyond duration: none -> ok
Program line: The Great Wisconsin Quilt Show (2026), per SST context
sections: 14 incl. 3 acts, Metadata Suggestions, Review Items for Formatter
```
self-report highlights:
- Prompt asks for exact char limits but analyst can't count (harness ban); lengths unmeasured.
- Program prefix lookup file unavailable; used SST program (correct here).
- **SST context carries an internal staff note with an @mention** (text kept in the private cohort) — passed to the model as data. Analyst treated it as context. Prompt-injection-shaped surface: SST free text reaches every phase verbatim.
- Title mismatch: SST "The Stock I Come From" vs spoken "The Stock That I Come From" (~00:07:58).
- Name conflict: "Fabu Carter" (caption label) vs "Fabu Phillis" (~00:38:56). Base: Edwards vs Kennedy (~00:27).
- SST social description has duplicate "the The Black Threads".

### formatter — prompts
`backend=openrouter model=None kwargs={'reasoning': {'enabled': False}} system=22744 user=115104 unfilled=[]`
Proper-noun style line: Catrina Sparkman, Edith Lawrence-Hilliard, Wanda Tapp, Cynthia Marie Woodland (+ surnames).
Supervisor check vs SRT:
- Speaker labels in this file are **dash-prefixed** (`- Catrina Sparkman:`, `- Fabu:`, `- Wanda Tapp:` …, ≥10 of them), yet intake reports `plain_name_labels: 1`. Either the intake counter misses `- Name:` form or counts only one distinct pattern — check against prod's detector (#449/#451 area).
- Proper-noun line **omits Fabu** (labelled `Fabu Carter` and `Fabu`, self-introduces as "Fabu Phillis"; the Madison poet laureate is Fabu Phillis Carter, so both are partials) and **Freida (High) Tesfagiorgis**, and "Black Threads". (#451)
- Dispatched to sonnet subagent.

### formatter — subagent (sonnet), 162 s, single call — **no classifier stop** (first long full-transcript formatter to get through on the round-3 brief)
gate:
```
completeness: {"is_complete": true, "source_word_count": 8420, "output_word_count": 8402, "coverage_ratio": 0.998, "threshold": 0.7, "skipped": false}
seam: {"has_gap": false, "blocking": false, "net_coverage_ratio": 0.9979, "captions_checked": 1176}
contract markers: review notes True; status needs_review True; ready_for_editing False; speaker 1 False; unidentified False
header: Project 6GWQ2603 | Program The Great Wisconsin Quilt Show | Duration 01:08:40
speaker labels: Announcer 2, Catrina Sparkman 12, Kay Simmons 3, Edith Lawrence-Hilliard 3, Wanda Tapp 2, Fabu Carter 6, Cynthia Marie Woodland 4, Attendee 1/2/3 1 each | source >> markers: 0
```
self-report highlights:
- Unlabeled turns attributed by context: 00:52:59 Catrina, 00:58:11 Edith, 00:59:22 Catrina.
- Fabu labelled "Fabu Carter" (caption label); spoken "Fabu Phillis".
- Style Rules forced "Cynthia Marie Woodland" label over caption "Cynthia Woodland".
- Words changed: okay→OK (3), serial commas removed, a few "you know" removed, one false-start cleanup (Edith 00:26:05).
- No-breaks-within-turn rule → Catrina's ~14-min opening is one block. Readability issue for editors.
- needs_review set (unlabeled turns, names, garbled "at least of them five master's degrees").
Supervisor: **Kay Simmons is a presenter (caption label `- Kay Simmons:`, self-intro "Kathryn Marie Simmons") but is also missing from the proper-noun line**, as are Fabu and Freida Tesfagiorgis. Formatter got all of them right from captions anyway.

### seo — prompts
`backend=openrouter model=None kwargs={} system=14353 user=22573 unfilled=['{the model you are running as}']` (PROMPT-1 variant spelling)
Keyword-candidates line = the same four presenter names only; no topical terms (quilt, Black Threads, storytelling). Dispatched to sonnet.

**Root cause of the missing names (supervisor):** `api/services/style_engine/entities.py` `_TABLE_ROW_RE` = `\|\s*([A-Z]… ){1,3})\s*\|` — the speaker cell must contain *only* 2–4 capitalized words. Analyst wrote `| Kay Simmons (Kathryn Marie Simmons) |` and `| Fabu (surname given as "Carter"… ) |` → no match → both presenters silently dropped from proper nouns AND SEO keyword candidates. Single-name speakers (Fabu) can never match ({1,3} requires ≥2 words). Any parenthetical in the name cell drops the row.

### seo — subagent (sonnet), 62 s
gate:
```
title [65/80] ok: Black Threads Quilting Collective story quilts and family history
short description [85/90] ok: Black Threads Quilting Collective members tell family histories through story quilts.
long description [297/350] ok: Members of the Black Threads Quilting Collective, founded by Catrina Sparkman, present six story quilts at the Great Wisconsin Quilt Show. Edith Lawrence-Hilliard, Wanda Tapp and Cynthia Marie Woodland are among the presenters tracing family lineage, generational memory and cultural preservation.
drafting-trail markers: []
status: ['draft'] | program: ['The Great Wisconsin Quilt Show']
```
Supervisor grounding:
- "founded by Catrina Sparkman" — from SST (long desc + social), **not** the transcript, which says "seven women artists came together" (00:03:59). SST-sourced, acceptable; flag for editor.
- "six story quilts" — matches six presenters. Olbrich, Baraboo, Creator's Cottage, MCSP, "161 years" all grep-confirmed in SRT.
- Long description names Edith, Wanda, Cynthia but **not Kay Simmons or Fabu** — exactly the two the extractor dropped from the verified-names line. Downstream effect of the entities.py bug.
- Header "Model: Sonnet 5.5 (claude-sonnet-5-5)" — model filled the unfilled `{the model you are running as}` placeholder from its own identity (PROMPT-1).
- SEO user.md transcript excerpt is the 2,000-char formatter head (#454); subagent noted it as truncated.

### validator — prompts
`backend=openrouter model=None kwargs={} system=4218 user=77272 unfilled=[]` → dispatched haiku.

### timestamp — prompts
`backend=openrouter model=None kwargs={} system=5300 user=95123 unfilled=[]`
Style block:
```
**Boundary candidates:** choose boundaries ONLY from this candidate list:
- 0:01 — [bright music] (music_cue)
```
**One candidate for a 68-min program.** Root cause: `pre_stage.py:600` `_SPEAKER_TRANSITION_RE = ^(?:>>\s*|\*\*Name:\*\*)` — this SRT's speaker convention is `- Name:` (e.g. `- Kay Simmons: Good evening.` at cue ~1264 line, `- Edith Lawrence-Hilliard:`, `- Wanda Tapp:`, `- Fabu Carter:`, `- Cynthia Woodland:`), none of which match. Same gap feeds intake's `plain_name_labels: 1`. Extends #449 (which covered the no-`>>` case). Expect a one-or-two-chapter result. Dispatched sonnet (run concurrently with validator — timestamp consumes only analyst+formatter, validator never sees timestamp, so data flow matches prod order).

### timestamp — subagent (sonnet), 41 s — no classifier stop
gate:
```
chapters: 1; SRT end 4120.120s
  0:00:00.000  'Episode intro'                                      on caption start  words=2
final end 1:08:40.000 vs SRT end 4120.120s
TS FAIL: fewer than 2 chapters on a >= 10-minute piece (see #449: empty boundary-candidate list)
```
self-report: followed the "authoritative" ONLY-from-candidates rule over the user prompt's "Identify 3-8 logical chapter breaks". Listed the boundaries it would use if allowed: Kay 0:14:37, Edith 0:23:53, Wanda 0:29:03, Fabu 0:34:26, Catrina's quilt 0:40:25, Cynthia 0:46:50, Q&A 0:52:59. (Prompt-internal conflict: Style Rules vs user task.)
Deliverable unusable (1 chapter / 68 min) → candidate for SUP-1 after QA gate.

### validator — subagent (haiku), 76 s
QA gate (prod path):
```
verdict: analyst pass | formatter FAIL (2 flags) | seo pass | overall fail
classify: escalate false, fixable [], nonfixable [2 flags]
PROD WOULD PAUSE: [qa_review] Paused for human review. ... Program header is incomplete ... 'The Great Wisconsin Quilt Show' vs SST 'The Great Wisconsin Quilt Show | 2026' ...; Dialogue paragraphs are not broken. Each speaker turn is one unbroken block ...
```
Supervisor adjudication:
- Flag 1 (Program header drops "| 2026"): **real but cosmetic**. Formatter template says `{Program name from manifest or derived from Media ID prefix}`; SST gave "… | 2026". Validator rule `validator/system.md:81` "matches the SST program". Either normalize the SST season suffix in code or tell the formatter to copy verbatim.
- Flag 2 (no paragraph breaks within turns): **false positive — prompt conflict.** Formatter `system.md:172` "Multi-speaker transcripts (most common): Do NOT add paragraph breaks within a single speaker's turn" and checklist `:354`; validator `system.md:58` "Paragraphs and sections are properly formatted" → Haiku read 14-min single blocks as a defect. Formatter obeyed its rule; validator penalizes it. Every long monologue job is exposed.
- Net: **prod would pause** on this job for (1) a cosmetic header mismatch and (2) a rule contradiction. Neither is a content error. SEO passed; no SEO length issues (measured 65/80, 85/90, 297/350).
- Single unit (no PRs) → no stack-vs-main routing comparison.

### SUP-1 — timestamp with `- Name:` speaker labels as candidates (NOT prod)
Change: `_SPEAKER_TRANSITION_RE` widened to `^(?:>>\s*|\*\*Name:\*\*|-\s*[A-Z][\w.'\s-]*:)`, run through the code-under-test's own `_extract_boundary_candidates` + `_subsample_candidates`; only the candidate block in `timestamp/user.md` replaced. Script: `sup1_candidates.py` (this folder). Result: 30 candidates (was 1). Prompt in `timestamp/supervised/`. Dispatched sonnet.
SUP-1 result — subagent (sonnet), 52 s:
```
chapters: 10; SRT end 4120.120s
  0:00:00.000  'Episode intro'
  0:00:18.000  'Welcome and the word "stock"'
  0:14:37.000  'Kay Simmons and "Re-Wind"'
  0:23:53.000  "Edith Lawrence-Hilliard's eight generations"
  0:29:03.000  "Wanda Tapp's train trips south"
  0:34:26.000  "Fabu's quilt for Mauricia"
  0:40:25.000  'Catrina Sparkman\'s "First Date"'
  0:46:50.000  "Cynthia Woodland's adoption story"
  0:53:05.000  'Researching family history'
  1:02:41.000  'Next generation and Cottage programs'
(all boundaries "NOT a caption start" — candidates rendered to whole seconds, #436)
```
Self-report: user prompt "3-8 chapters" vs Style Rules "at most 10" → took 10; 18-s intro chapter; Q&A actually starts 52:59 (unlabeled, not a candidate); analyst context truncated at 4000 chars (`worker.py:3848`).

## Close
- Single unit, no stack comparison. All subagent; no classifier stops; no OpenRouter spend.
- FINDINGS-6GWQ2603.md written.
