# What the analyst produces, and what each downstream phase consumes

Research for [#425](https://github.com/public-media-work/cardigan/issues/425), part of the
wayfinder map [#423](https://github.com/public-media-work/cardigan/issues/423). Snapshot of
`origin/main` at `dc7bbea` (2026-10-08).

**Sources:** primary only. The prompts in `prompts/*.md`, the prompt assembly in
`api/services/worker.py`, the style engine in `api/services/style_engine/`, the routing
config in `config/llm-config.json`, the rule file `config/house_style.yaml`, and the MCP
server in `mcp_server/server.py`. One recorded run, `test-runs/2026-09-24-6POL0213-manual-supervisor/`,
is used for a size measurement. No production API or AirTable calls were made.

Line numbers refer to `api/services/worker.py` unless a file is named.

---

## 1. How context moves between phases today

The worker runs `PHASES = ["analyst", "formatter", "seo", "validator"]` in order, then the
optional `timestamp` (`worker.py:237–241`). It keeps one `context` dict per job
(`worker.py:968–977`). After each phase succeeds, it sets `context[f"{phase}_output"]` to the
**whole raw markdown** of that phase (`worker.py:1075`). No later phase parses the analyst
document into fields. Each phase's user message pastes in whatever earlier outputs its branch of
`_build_phase_prompt_base` (`worker.py:3331–3650`) chooses, sometimes cut to a fixed length.

There is one exception. The style engine's pre-stage parses the analyst document **deterministically**.
`style_engine/entities.py:extract_proper_nouns` pulls every 2–4-word capitalised name out of the
**Speakers & Roles** table. Those names are then injected into the formatter and seo prompts as
"Proper nouns (authoritative spellings — use exactly these)" (`style_engine/pre_stage.py:146–160, 234–278, 352–370`).
`routing.style_engine.phases.*.pre` is `true` for every phase in `config/llm-config.json`, so
this path runs today. Post-stages and lint are `"off"`, so the analyst-driven casing canonicaliser
in `post_stage.py:217–218` does **not** run.

Phase inputs other than the analyst output:

| Input | analyst | formatter | seo | validator | timestamp |
|---|---|---|---|---|---|
| SST block (`worker.py:3336–3361`) | yes | yes; on chunked runs **chunk 1 only** (`2936–2956` has no SST) | yes | yes (#373) | yes |
| `knowledge/glossary.md` (`3366–3371`, `2840–2844`) | yes | yes (every chunk) | **no** | **no** | **no** |
| Diarization segments (`3430–3454`) | no | unchunked path only, first 50 segments | no | no | no |
| Style pre-stage section | verified facts (duration, words, project, file), draft char budgets, voice | proper nouns from the analyst table, speaker-label spec, enforce substitutions | char budgets, analyst proper nouns, proper nouns verified present in the transcript, voice | deterministic-check summary (empty today: needs `style_checks`, which only enforce-mode post writes) | SRT end time, max chapters, ≤60 boundary candidates from `>>` / music cues |
| Completeness / seam results | — | — | — | completeness ratio | — |
| Editorial feedback on retry | yes | unchunked only | yes | **no branch** | yes |
| Validation flags + previous output on retry (`3309–3320`) | yes | unchunked only | yes | yes | yes |

The chunked formatter (`_run_formatter_chunked`, `2816–3170`) builds its own messages. It never
reads `_editorial_feedback`, `_validation_flags`, `_previous_output` or `diarization_result`.
This matches RETRY-1 in `test-runs/…/FINDINGS-6POL0213.md`, and it is still true on `dc7bbea`.

`context["program"]` is never set by the worker. Every `rules.limits_for(program, …)` and
`rules.program_rules(program)` call in the pre-stage therefore uses the default, not
program-specific rules.

---

## 2. How much transcript each phase receives

| Phase | Raw transcript | Formatted transcript | Analyst output |
|---|---|---|---|
| **analyst** | **Whole.** The raw file text, SRT timecodes included (`_load_transcript`, `1988–2023`). | — | — |
| **formatter** | **Whole, chunked above 3,000 dialogue words**: ~2,000-word chunks, 8-caption overlap, 3 in parallel (`routing.chunking`, `chunking.py:split_transcript`). Interior `>>` turns are split first (`2343–2349`). | — | **Whole**, repeated in every chunk |
| **seo** | **None** (the pre-stage checks analyst names against the full raw text) | **First 2,000 characters only** (`3487`), about 350 words or 2–3 minutes of talk | **Whole** |
| **validator** | **None** | **Whole** | **Whole** |
| **timestamp** | **Whole** SRT, capped at 500,000 chars (`3611`) | **None.** `formatted` is read at `3579` but never put in the prompt, though `prompts/timestamp.md:11` lists it as an input. | **First 4,000 characters only** (`3616`) |

**Measured truncation.** In the recorded 6POL0213 run (18:04, 3,333 dialogue words) the analyst
document was 16,516 bytes. Its section offsets were Summary 190, Key Themes 1,120,
**Speakers & Roles 3,448, Structural Breakdown 4,336**, Key Quotes 8,267, SEO Keywords 10,648,
Editorial Opportunities 11,450, Metadata Suggestions 12,580, Production Notes 14,830 and
Review Items 15,657. The 4,000-character cut handed to timestamp stopped **partway through the
speaker table and before any of the Structural Breakdown**. The worker labels that block
"use this to identify chapter boundaries" (`3615`).

---

## 3. The matrix: analyst section → downstream receipt → actual use

Legend: **R** = the text reaches the phase's prompt. **Used** = the downstream prompt tells
the model to do something with it, or code parses it. "Implicit" means the text is present but
no instruction names it.

| Analyst section (`prompts/analyst.md`) | formatter | seo | validator | timestamp | Off-pipeline (MCP / copy-editor) |
|---|---|---|---|---|---|
| **Header** (Project, Program, Date, Model) | R. **De facto used**: the formatter gets no project name or manifest, yet `formatter.md:86–87` asks for "Project … from filename" and "Program from manifest". The analyst header is the only source of either on the unchunked path. | R. **De facto used**, same reason (`seo.md:54` "from project manifest"; no manifest is sent) | R. Checks Program against SST (`house_style.yaml` checklist) | R (within 4k). No instruction. | Shown |
| **Summary** | R. Not used | R. Implicit: feeds title and description accuracy | R. **Used**: "title/description accurately reflects content" is checked against it, because the validator has no raw transcript | R. Implicit | Used ("summarize themes") |
| **Key Themes** | R. Not used | R. **Used** (`seo.md:272` "pull … themes from brainstorming doc") | R. **Used** (checklist: "Key themes … identified") | R (within 4k). Implicit | Used |
| **Speakers & Roles** table | R. **Used twice**: by prompt (`formatter.md:17, 43, 156`) and **deterministically** as the authoritative proper-noun list in every chunk | R. **Used deterministically** (proper nouns + transcript-verified keyword candidates) | R. **Used** ("Speaker identification is present") | **Cut off at 4k** in the measured run. Not named by the prompt | Used ("roles/titles: look up in brainstorming doc") |
| **Structural Breakdown** (acts with time ranges) | R. **Not used**: the formatter is told to add no structure (`formatter.md:193–201`) | R. Not used | R. **Used** ("Segment count is reasonable for content duration") | **Intended consumer, but past the 4k cut** in the measured run | — |
| **Key Quotes & Moments** | R. Not used | R. Implicit at most ("Quote Cards", `seo.md:191`) | R. Not checked | Usually past 4k | **Used** (`EDITOR_AGENT_INSTRUCTIONS.md:463`) |
| **SEO Keywords (Preliminary)** | R. Not used | R. **Used** (`seo.md:12`, `:272`; analyst: "refined by the SEO agent") | R. Not checked (validator judges SEO keywords) | Past 4k | Used |
| **Editorial Opportunities** (hook, angle, audience, searchability) | R. Not used | R. Implicit only | R. Not checked | Past 4k | Used |
| **Metadata Suggestions** (draft title/short/long, `{{style:analyst.draft_guidance}}`) | R. **Explicitly forbidden** (`formatter.md:69`) | R. **Used** (`seo.md:12` "metadata drafts") | R. Not checked | Past 4k | **Used** (`review_brainstorming` prompt, `mcp_server/server.py:891–990`) |
| **Production Notes** (caption quality, live-caption source) | R. Weakly used: the analyst says this is "so the formatter knows to expect errors" (`analyst.md:57`), but `formatter.md` never names the section | R. Not used | R. Not checked | Past 4k | Shown |
| **Review Items for Formatter** (attribution, spelling, gaps) | R. **Used generically** (`formatter.md:211–250` "issues the brainstorming document doesn't resolve") | R. Not used | R. Not checked | Past 4k | Shown |

**Reading across the rows:**

- **One section does almost all the cross-phase work: the speaker table.** It is the only
  section code parses, and it is the main path for names in the formatter and seo.
- **Of the 11 sections, the formatter acts on 3** (header, speakers, review items) and is
  barred from one (metadata suggestions). It still receives the whole document in every chunk.
- **SEO uses the most**: themes, keywords, metadata drafts, the speaker list and the header.
  It also leans on the analyst instead of the transcript, because it sees only 2,000 characters
  of the formatted text.
- **Timestamp's intended input (Structural Breakdown) usually never arrives**, because of the
  4k cut. Chapters actually come from the raw SRT plus the deterministic boundary candidates.
- **Key Quotes, Editorial Opportunities and Production Notes have no pipeline consumer that
  acts on them.** Their real consumer is the human or the Claude Desktop copy-editor through MCP.

---

## 4. Which corrections travel forward (the path per-item generators lose)

Follow-up from the [podcast-pipeline comparison note](https://github.com/public-media-work/cardigan/issues/423#issuecomment-6064810976),
point 6. "Travel" means a correction made or applied in one place reaches a later phase's prompt.

| Correction | Where it enters | Reaches | Does not reach |
|---|---|---|---|
| **Speaker names from SST** (Host/Presenter, plus regex extraction from Social Media Description and Project Notes, `_extract_speakers_from_sst`, `109–163`, applied at `1686–1691`) | SST block | Every phase directly | Continuation chunks of a chunked formatter. Those get names **only** through the analyst's speaker table and the pre-stage proper-noun list |
| **Analyst speaker resolution** (e.g. live-caption `>>` mapped to "Shawn Johnson") | Speakers & Roles table | formatter (prompt + deterministic list, all chunks), seo (prompt + deterministic list), validator | timestamp, when the table falls past the 4k cut. Chapter titles that name people then rely on SST plus raw captions |
| **Glossary spellings** (`knowledge/glossary.md`, e.g. Sean→Shawn) | analyst and formatter prompts | seo, validator and timestamp **only indirectly**: through names the analyst wrote into its table or keywords, or through the first 2,000 chars of formatter output (seo) | seo, validator and timestamp never see the glossary itself. A glossary term the analyst did not repeat cannot be enforced in titles, keywords or chapter names |
| **Formatter's in-body fixes** (garbled proper nouns, caption errors) | formatter output | validator (whole); seo (**first 2,000 chars only**) | timestamp (never gets formatter output). seo for any fix after the opening minutes |
| **Diarization speaker turns** | formatter, unchunked only | Indirectly, through formatter output | Chunked formatter runs, which is any episode over ~3,000 dialogue words |
| **Editorial feedback on a retried phase** | that phase only (`retry_single_phase`, `430–620`) | That phase. `_extract_glossary_entries` (`683–788`) appends name corrections to `glossary.md`, so **future** analyst and formatter runs see them | Other phases of the same job: retrying the analyst does not re-run formatter or seo. The chunked formatter ignores feedback entirely. The validator has no feedback branch |

**Takeaway for the new model.** Today a name correction travels forward in three ways: the
SST block, the analyst's speaker table (also parsed into the authoritative proper-noun list),
and the glossary for the two phases that get it. All three are **reference data, not
phase-to-phase prose**. They can move into the context bundle as explicit, versioned slices.
Per-item generators then get the corrections directly instead of hoping an upstream phase
repeated them. The phased chain's indirect path is already lossy: timestamp misses most of it,
seo sees 2,000 characters of the corrected transcript, and chunked formatting drops SST and
diarization after chunk 1.

---

## 5. Candidate context bundle slices

Each slice is marked **D** (deterministic, computed without a model) or **I** (inferred, the
part the analyst produces today).

| # | Slice | D/I | Built from | Replaces today's |
|---|---|---|---|---|
| B1 | **Job facts**: media ID, program (from prefix map and SST), duration from the SRT, word count, content type, source file | D | filename, `knowledge/Media ID Prefixes.md`, SRT parse, `_detect_content_type` | Analyst header, style pre-stage "verified facts", `context["program"]` (never set today) |
| B2 | **Parsed captions**: cue list with timecodes, `>>` turns split, dialogue-only text | D | `utils.parse_srt`, `speaker_segmentation` | Raw transcript pasted whole or chunked |
| B3 | **SST record** (read-only snapshot, re-fetched at push) | D | `_fetch_sst_context` | SST block, missing from formatter continuation chunks |
| B4 | **Name authority**: canonical people and roles, merged from SST host/panel, the glossary and resolved speakers, each with a `source` | D for SST/glossary entries, I for transcript-only resolutions | `_extract_speakers_from_sst`, `glossary.md`, analyst speaker table | Speakers & Roles table, pre-stage proper nouns, glossary |
| B5 | **Speaker map**: caption-turn → canonical speaker (with diarization when present) | I (D-assisted) | analyst speaker resolution, diarization | Speaker table, the formatter's attribution work, diarization reaching only unchunked formatter |
| B6 | **Glossary / house substitutions** | D | `glossary.md`, `house_style.yaml` enforce tier | Glossary sent to analyst and formatter only |
| B7 | **Content outline**: summary, themes, segments with time ranges | I | today's Summary, Key Themes, Structural Breakdown | Those three sections, including the Structural Breakdown that timestamp never receives in full |
| B8 | **Keyword seed**: preliminary keywords with `direct` (spoken, verified against B2) or `implied` | I, with D verification | today's SEO Keywords (Preliminary), pre-stage keyword candidates | SEO Keywords section |
| B9 | **Caption-quality notes**: live-caption source, gaps, uncertain spellings | I (several detections are D: `>>` density, gaps in timecodes) | Production Notes, Review Items | Two sections no generator acts on reliably |
| B10 | **Formatted transcript** as a bundle slice for later items (the deterministic transcript item, if it exists) | D or I | formatter item | The 2,000-char seo excerpt |

**Drop, or keep as human-only artifacts:** Editorial Opportunities, Key Quotes and Metadata
Suggestions (the draft copy). Only the SEO phase and the MCP copy-editor read them. In the new
model a copy generator writes its own suggestion, and an editor-facing "brainstorm" item can
stay as an item rather than as bundle input.

---

## 6. Which future generators can be scoped tightly

| Generator (item) | Needs | Whole transcript? |
|---|---|---|
| **Formatted transcript** | B2, B4, B5, B6, B9 (a segment at a time) | Whole **coverage**, but it can be done segment by segment. Each segment gets B4/B5/B6 directly, not "chunk 1 gets SST". Does not need B7/B8 |
| **Chapters / timestamps** | B1, B2 (timecodes plus boundary candidates), B7 segments, B4 for names in titles | Cue timings and outline only. Today it gets the whole raw SRT and a truncated analyst; the outline slice is the missing piece |
| **Title** | B1, B3, B4, B7 summary/themes, B8 | No |
| **Short / long description** | B1, B3, B4, B7, B8 | No (B7 summary stands in; optionally a few verified quote spans) |
| **Keywords / tags** | B1, B3 keywords, B4, B8, verification against B2 | No. Verification is a deterministic string check over B2 |
| **Social hashtags** | B1, B8 | No |
| **Speaker list / credits** | B3, B4, B5 | No |
| **Semantic QA** (title or description reflects the content) | the item under review, B7, B3, B4 | No. Today the validator gets the whole formatted transcript. With B7 and B4 it can judge one item against the outline and name authority |

**Only two parts need the whole transcript:** building B5/B7/B8 (the analyst's job, done once
per job) and the formatted-transcript item. Every copy item can be scoped to B1, B3, B4, B7 and
B8, which together run to a few kilobytes. That is less than today's seo prompt, which sends the
full ~16 KB analyst document plus 2 KB of transcript.

---

## 7. Defects observed along the way (not fixed here)

These are on `dc7bbea` and relevant to the replacement, not proposed as hotfixes:

1. `timestamp` reads `formatter_output` (`3579`) but never puts it in the prompt, contradicting `prompts/timestamp.md:11`.
2. The `timestamp` prompt cuts the analyst document at 4,000 chars (`3616`). In the measured run that dropped the Structural Breakdown it is told to use.
3. Formatter continuation chunks get no SST block (`2933–2956`). Names on chunks 2+ come only from the analyst table.
4. The chunked formatter ignores diarization, editorial feedback, validation flags and previous output (RETRY-1, still open).
5. `context["program"]` is never populated, so program-specific style limits and formulas never apply.
6. `formatter.md` and `seo.md` list a "project manifest" input the worker never sends. The analyst header is the real carrier.
7. The glossary never reaches seo, validator or timestamp.
