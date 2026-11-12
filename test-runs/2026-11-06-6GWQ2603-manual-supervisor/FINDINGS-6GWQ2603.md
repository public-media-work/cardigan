# FINDINGS — 6GWQ2603 "The Stock I Come From" (Great Wisconsin Quilt Show 2026)

Supervised run, 2026-11-06. Code: `origin/main` @ `262bdc7` (= cardigan02 prod), no PRs.
Backend: **all five phases on Claude Code subagents** (analyst/validator haiku, formatter/seo/timestamp sonnet). No classifier stops, no OpenRouter calls, $0 API spend. Input: 68.7 min, 1176 captions, 8420 dialogue words, single-call formatter, SST record present.

## 1. Bottom line

**Prod would PAUSE for QA review** (`[qa_review]`, nonfixable, no escalation) — on two validator flags, neither of which is a content error:
1. Formatter's `**Program:**` header drops SST's `| 2026` suffix (real, cosmetic).
2. "Speaker turns are unbroken blocks" — the formatter **obeyed** its own rule; the validator prompt contradicts it (false positive).

What would reach the deliverable wrong if an editor resumed:
- **Timestamps: one chapter (`0:00 Episode intro`) for a 68-minute program.** Validator never sees timestamp output, so nothing catches it.
- SEO long description names three of the six presenters, omitting Kay Simmons and Fabu, who are the two people the proper-noun extractor dropped.

Formatter itself was strong: 99.8% coverage, no seam gaps, correct speaker labels for all 6 presenters + 3 attendees, conservative edits.

## 2. Defects

| ID | Sev | Tier | Where | What | Fix |
|---|---|---|---|---|---|
| TS-3b | P1 | hotfix | `api/services/style_engine/pre_stage.py:600` | `_SPEAKER_TRANSITION_RE` accepts `>>` and `**Name:**` only; this SRT uses `- Name:` labels (30 transitions). Candidate list = one music cue at 0:01 → prompt says "ONLY from this list" → 1 chapter. | Accept `^-?\s*[A-Z][\w.'\s-]*:` labels. SUP-1 (exactly this regex widening) gave 10 well-placed chapters on presenter changes. Also #449's "drop ONLY when list is tiny" and the <2-chapter post-check. |
| STY-2b | P1 | hotfix | `api/services/style_engine/entities.py` `_TABLE_ROW_RE` | Speaker cell must be *only* 2–4 capitalized words. `\| Kay Simmons (Kathryn Marie Simmons) \|` and `\| Fabu (surname given as …) \|` → dropped. Single-word names can never match. Effect: proper-noun line and SEO "verified keyword candidates" omit 2 of 6 presenters; SEO long description omits them. | Strip parentheticals before matching; allow single-word names. Add this table as a fixture. |
| VAL-P | P1 | hotfix | validator prompt `system.md:58` vs formatter prompt `system.md:172,354` | Formatter: "Multi-speaker transcripts: Do NOT add paragraph breaks within a single speaker's turn". Validator: "Paragraphs and sections are properly formatted". Haiku flagged a 14-min single-turn block as a formatter fail → prod pause. Any monologue-heavy job (presentations, lectures) is exposed. | Tell the validator explicitly that one block per speaker turn is correct and is not a flag. |
| PROG-H | P2 | v-next | formatter template `**Program:** {Program name from manifest…}` vs validator `system.md:81` | SST program is "The Great Wisconsin Quilt Show \| 2026"; formatter wrote it without the season suffix; validator demands exact SST match → fail. | Either inject the SST program value into the header deterministically (no model involvement) or have the validator ignore a trailing `\| YYYY`. Deterministic header is preferred. |
| TS-ctx | P2 | v-next | `worker.py:3848` | Timestamp prompt gets `analysis[:4000]`; this analyst doc is longer, so the structural breakdown was cut (subagent reported it). | Pass the Structural Breakdown section rather than a byte prefix. |
| SST-note | P2 | v-next | SST context injection (all phases) | SST long-description field contains an internal staff note with an @mention (text kept in the private cohort). It reaches every prompt verbatim. Models treated it as context this time. Free text from Airtable, which staff can edit, ends up in every phase's prompt with no marking. | Strip `@mention` notes, or fence SST as quoted data. Low risk today. |
| H-1 | P2 | harness | `test-runs/harness.py` intake | `plain_name_labels: 1` despite 30 `- Name:` labels; intake mirrors the same narrow detection. | Count dash-prefixed labels. |

Known, not re-filed: PROMPT-1 (#410) unfilled `{model name…}` placeholder in analyst/SEO; the SEO subagent filled it with its own identity. #454 (SEO gets only `formatter_output[:2000]`).

## 3. Regression vs open issues

| Issue | Status here |
|---|---|
| #449 TS-3 no boundary candidates on non-`>>` captions | **Reproduced**, new variant: dash-prefixed `- Name:` labels. Comment-worthy. |
| #451 STY-2 extract_proper_nouns drops names | **Reproduced**: parenthetical (already listed) + **single-word name** (new). |
| #436 snapping boundaries to cues | Seen in SUP-1: candidates rendered to whole seconds → gate "NOT a caption start" on all 9 boundaries. |
| #454 SEO-3 `[:2000]` | Present; harmless here because SST + analyst carried the facts. |
| #452 PROG-1 Program guessed | Not triggered (SST present). |
| #450 ESC-1 | Not triggered (no escalation). |
| #404 word count | job_word_count 13124 vs dialogue 8420; single call was fine (32k cap). |
| #130 multi-paragraph speaker blocks (closed) | Formatter rule holds; the **validator** now contradicts it (VAL-P, new). |

## 4. What went right

- **No classifier stops** on a long (8.4k-word) single-call Sonnet formatter run on the round-3 brief, the first long-transcript formatter to get through. Timestamp also clean.
- Formatter: 99.8% coverage, all speakers correctly labelled from caption labels despite the proper-noun gaps; flagged the right uncertain items (unlabeled Q&A turns, Fabu Carter/Phillis, Edwards vs Kennedy AFB).
- SEO: all fields within limits (65/80, 85/90, 297/350); every fact grep-confirmed in SRT or sourced from SST ("founded by Catrina Sparkman" is SST; the transcript says "seven women artists came together").
- Analyst: correct program from SST; caught the title mismatch (SST "The Stock I Come From" vs spoken "The Stock That I Come From") and the SST typo "the The Black Threads".

## 5. Recommendation

Three small hotfixes would turn this job from "pause + 1 chapter" into "complete with useful chapters": widen `_SPEAKER_TRANSITION_RE` for `- Name:` (TS-3b), strip parentheticals / allow single names in `entities.py` (STY-2b), and remove the paragraph contradiction from the validator prompt (VAL-P). Make the Program header deterministic in v-next.

For this episode's editors, check: title choice (SST vs spoken "That"), Fabu Phillis Carter's name, Edwards vs Kennedy AFB (1997 ceremony was at Kennedy Space Center), lynching statistics, and add chapters from SUP-1.

## Caveats

- Subagent wall time ≈10× an API call; subagents had file tools prod's model lacks.
- No `finish_reason`/reasoning-token data (no replay phases). Prod's formatter runs with reasoning disabled; the subagent's equivalent isn't controllable.
- Validator/formatter results are single samples; Haiku flag behaviour may vary run to run.
