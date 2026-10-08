# Output inventory: what comes from a caption file, and what needs inference

Research for #424, on the map #423 (*Cardigan — one job on the new model*).

**Question.** For every output the current pipeline produces, and every SST field
in `config/sst_field_map.json`, can it be produced **deterministically** from a timed
caption file (SRT/SCC) plus the SST record and glossary when present? Or does it need
**inference**? Or is it **split** between the two?

**Method.** I read the repo on `origin/main` at `dc7bbea` (2026-10-08). Sources were
the worker, the prompts, the style engine, the MCP server and the house-style config.
I did not call production or AirTable. Every claim below names the file it comes from.
"Today" means what the code does now. "Could be" means what the inputs already allow.

---

## Definitions used here

- **Deterministic.** The output can be computed by code from the parsed captions,
  the SST record, the glossary, `config/house_style.yaml` and the Media ID. Running
  it twice on the same inputs gives the same result.
- **Inferred.** The output needs a judgement about meaning: what matters, what to
  call it, how to describe it. A model (or a person) has to make that call.
- **Split.** Part of the output is deterministic and part is inferred. Each row says
  where the line falls, so the item definition can give each part its own generator.

---

## Headline findings

1. **Most of the deterministic machinery already exists, but production has it
   switched off.** `api/services/style_engine/` has code for chapter snapping,
   Media Manager and YouTube timecode rendering, down-style casing, limit truncation,
   house-style substitutions and a deterministic validator checklist. In
   `config/llm-config.json` → `routing.style_engine.phases`, every phase runs with
   `"post": "off"` (and `"lint": "off"` for the validator). `resolve_prompt_profile`
   (`style_engine/prompt_blocks.py:155`) returns `"full"` unless the mode is
   `"enforce"`. So today the timestamp model writes the Media Manager table and the
   YouTube list itself, end-time arithmetic included, even though
   `timecodes.emit_media_manager_table` already does that math exactly.
2. **Only the timestamp phase reads the timed captions.** analyst, formatter, seo and
   validator all get the transcript as text. The SRT itself goes only to the timestamp
   prompt (`worker.py` ~1242–1253, `_build_phase_prompt_base` timestamp branch) and to
   the deterministic helpers: duration, chunking, speaker segmentation, seam coverage
   and timestamp candidates. The SEO phase sees only `formatted[:2000]` of the
   transcript (`worker.py`, seo branch). It works mainly from the analyst's summary.
3. **SCC is not parsed anywhere in the pipeline.** `api/services/utils.py` has
   `parse_srt` and nothing for SCC. SCC shows up only as an indexed sidecar type in
   `mmingest` (`indexer.py:48`, `_SIDECAR_FILE_TYPES`). The map treats SRT and SCC
   as canonical input, so an SCC→cue parser is a missing prerequisite for every
   deterministic item below.
4. **Chapters are the clearest split item.** Boundary *candidates* are already
   deterministic: `pre_stage._extract_boundary_candidates` collects `>>` speaker
   transitions, bold speaker labels and music cues. The chapter cap
   (`phases.timestamp.chapter_max_by_duration`), the forced 0:00 "Episode intro",
   snapping and both output formats are deterministic too. Only two judgements are
   left: which candidates mark a real topic shift, and what each chapter is called.
   The comparison note's point 5 ("snap to the first cue after the re-intro") is the
   same shape as `snap_chapters`. It needs one more rule, "snap forward to the next
   speaker-transition or music cue", which the code does not have yet.
5. **The formatted transcript is mostly deterministic, with one hard inference
   inside it: assigning names to anonymous `>>` turns.** Splitting turns
   (`speaker_segmentation.py`), house-style substitutions (`substitutions.py`), label
   and spacing normalization, and the content-loss checks (`completeness.py`,
   `seam_coverage.py`) are already code. Filler removal is a word list. For
   post-production captions, which are already punctuated, the rest is mechanical.
   Live captions add two inferred pieces: punctuation and garble repair, and picking
   a speaker name for each `>>` turn. The comparison note's point 4 (0.00% word delta
   from a deterministic formatter) supports splitting the item along this line.
6. **Every copy field is inferred.** Release Title, Short Description, Long
   Description and Facebook Description all need inference. Only the checks around
   them are deterministic: limits, casing, forbidden-phrase and person-voice scans.
   The one partial exception is Here & Now, whose `programs` block in
   `house_style.yaml` gives formula templates (`"{subject} on {topic}"`). The slots
   are still inferred.
7. **Keywords, tags and hashtags are split.** The proper nouns found in the
   transcript (`entities.extract_proper_nouns`, then the `keyword_candidates` filter
   in `pre_stage.py:157`) and the branded tags (`PBS Wisconsin`, program name,
   `Wisconsin`, `#Shorts`) are deterministic. The "implied" search-intent terms are
   inferred, and so is the ranking.

---

## A. Pipeline artifacts, section by section

Phases come from `api/services/worker.py`: `PHASES = ["analyst", "formatter", "seo",
"validator"]` and `OPTIONAL_PHASES = ["timestamp"]`. Artifact filenames are listed in
`_create_manifest` and in `api/routers/jobs.py:516–522`.

### A1. `analyst_output.md` (brainstorming document)

Producer: `prompts/analyst.md`, given the transcript text, the SST section and the
glossary (`_build_phase_prompt_base`, analyst branch). The analyst pre-stage adds
verified facts: duration, word count, project and file (`pre_stage.py:481`). The map
already treats this as **context, not a deliverable**: its standing decision folds the
analyst's inferred parts into the shared context bundle.

| Section | Class | What it uses today | Notes |
|---|---|---|---|
| Header: Project, Program, Date, Model | **deterministic** | Project is passed in as a verified fact; the model fills in Program, Date and Model | Program can be looked up from the Media ID prefix (`mmingest/media_id_prefixes.yaml`, `utils.extract_media_id`) or the SST Project link (`SST_PROJECT_FIELD_MAP`). The date and model are already substituted by `_load_agent_prompt`. |
| Summary | **inferred** | Whole transcript | |
| Key Themes | **inferred** | Whole transcript | |
| Speakers & Roles | **split** | Transcript, SST Host/Presenter/Social Media Description/Project Notes, glossary | Names are deterministic when SST lists them. `_extract_speakers_from_sst` (`worker.py:109`) already parses host and panelists with regex. "First Appearance" is a caption timecode once turns have names. Role and context are inferred. Matching anonymous `>>` turns to names is inferred. |
| Structural Breakdown | **split** | Transcript | Segment edges can come from the same caption cues the timestamp pre-stage uses. Act labels and the bullet summaries are inferred. |
| Key Quotes & Moments | **split** | Transcript | Picking the quotes is inferred. Each quote's verbatim text and timecode can be looked up in the captions, and that check would catch misquotes. |
| SEO Keywords (Preliminary) | **split** | Transcript, SST keywords | Proper-noun and location candidates are deterministic (`entities.extract_proper_nouns`; `casing.proper_nouns` in `house_style.yaml` as a Wisconsin gazetteer). Topical and implied terms are inferred. |
| Editorial Opportunities | **inferred** | Transcript | |
| Metadata Suggestions (draft title/SD/LD) | **inferred** | `{{style:analyst.draft_guidance}}` | Duplicates the SEO phase's work. The draft limits are deterministic. |
| Production Notes | **split** | Transcript | Caption-source detection can be code: live captions show `>>` markers, all-caps text and doubled words, as the analyst prompt itself lists. The rest of the commentary is inferred. |
| Review Items for Formatter | **split** | Transcript, glossary | Timecode gaps are deterministic (gaps between cues). A proper noun that is not in the glossary or the SST is a deterministic flag. Attribution doubts are inferred. |

### A2. `formatter_output.md` (formatted transcript)

Producer: `prompts/formatter.md`, given the transcript text, `analyst_output`, SST, the
glossary and the optional diarization segments (formatter branch). Long transcripts
are chunked (`chunking.py`, at 3,000 words or more) through `_run_formatter_chunked`.
Live-caption SRTs are split first by `speaker_segmentation.split_interior_speaker_changes`.
The output has **no AirTable target** (`sst_field_map.json` → `unmapped_producers`;
gap G3).

| Part | Class | Evidence |
|---|---|---|
| Header (Project, Program, Duration, Date) | **deterministic** | Duration from `utils.get_srt_duration`. Project and Program as in A1. The prompt currently has the model "calculate from SRT timecodes if needed". |
| Turn segmentation (one block per speaker turn) | **deterministic** | `>>` and label markers in the caption text; `speaker_segmentation.py` already splits interior `>>`. With diarization it comes from the audio (`diarization_client.py`), not the captions. |
| Speaker *names* on turns | **split** | Deterministic when the captions carry names (`NAME:`) or the episode has a single known speaker from SST. **Inferred** when live `>>` turns have to be matched to SST names. This is the attribution the formatter prompt spends most of its rules on. |
| Dialogue text: filler removal, house-style substitutions, glossary spellings, de-italicized program names, label format and spacing | **deterministic** | `house_style.yaml` → `phases.formatter.substitutions` and `speaker_label`; `style_engine/substitutions.py` (`apply_substitutions`, `normalize_speaker_turns`). Glossary entries are wrong→right pairs (`glossary.py`). Filler removal would be a word list; no such list exists in the code yet. |
| Punctuation and grammar repair | **split** | Post-production captions are already punctuated, so the work is close to a no-op. Live captions (mostly unpunctuated, false starts) need inference. |
| Garbled-caption reconstruction | **inferred** | Formatter prompt: "include your best reconstruction rather than dropping it". |
| Review notes (HTML comment) | **split** | Unknown speaker or a proper noun missing from the glossary are deterministic flags. Judgement about uncertain spellings is inferred. |
| Status (`ready_for_editing` / `needs_review`) | **deterministic** | Follows from whether any review notes exist. |
| Content-loss checks (not output, but they gate it) | **deterministic** | `completeness.py` (word ratio ≥0.70) and `seam_coverage.py` (trigram anchoring). Comparison-note point 4 checks (6-word sentence sampling, 25-word reverse coverage, proper-noun diff) would fit here. |

### A3. `seo_output.md` (SEO report)

Producer: `prompts/seo.md`, given `analyst_output`, the **first 2,000 characters** of
`formatter_output`, SST and an optional `keyword_report_v*.md` (seo branch). The
pre-stage adds character budgets, transcript-verified keyword candidates and the
program formula (`pre_stage.py:352`).

| Section | Class | Notes |
|---|---|---|
| Title (recommended + alternatives) | **inferred** | Deterministic around it: the 80-character limit, down-style casing (`casing.to_down_style`), the Here & Now `title_format` and `livestream_title_suffix`, and forbidden-phrase and person-voice scans. |
| Short Description | **inferred** | 90-character limit with deterministic word-boundary truncation (`post_stage`, enforce tier, currently off). Here & Now `short_description_formula`. |
| Long Description | **inferred** | 350-character limit. Here & Now `long_description_formula`. |
| "Keywords Included" / "Reasoning" sub-blocks | **split** | "Keywords included" is a deterministic string match of keywords against the copy. The reasoning is inferred, and no longer needed once limits are code. |
| Keyword Strategy tiers (Primary/Secondary/Long-Tail/Location) | **split** | The `Source: direct` rows are deterministic: the term occurs in the transcript, and pre-stage `keyword_candidates` already compute this. `implied` rows, relevance scores and long-tail questions are inferred. Location keywords are deterministic against a gazetteer. |
| YouTube Tags | **split** | Branded tags (program name, `PBS Wisconsin`, `Wisconsin`) and direct proper nouns are deterministic. Topical tags and their order are inferred. Competes with the tiers for `General Keywords/Tags` (gap G1). |
| Social Media Hashtags | **split** | `#Wisconsin`, `#<ProgramName>` and `#Shorts` (for content_type `short`) are deterministic. Topic hashtags are inferred. |
| Platform-Specific Recommendations (thumbnail, playlist, category, posting) | **inferred** | Playlist and category could be looked up from the program, but there is no table for that today. |
| SEMRush Integration | **deterministic** | Only when the user uploads data (`jobs.py` keyword-report upload stores the CSV inside markdown). Re-tabulating data is parsing; deciding which keywords to prioritize is inferred. |
| Accessibility & Inclusivity | **split** | Whether a transcript or captions exist is deterministic. Alt-text suggestions are inferred. |
| Quality Score | **inferred** | The model scoring its own output. Context only (`sst_field_map.json`: `context_confirmed`). Deterministic limit and lint results would be more trustworthy. |
| Next Steps, Status | **deterministic** | Fixed boilerplate. |

### A4. `validator_output.md` (QA verdict)

Producer: `prompts/validator.md` (JSON verdict) plus `{{style:validator.checklist}}`,
merged with style-engine flags (`qa_merge.merge_style_flags`) and the completeness
result. The map dissolves this into per-item status.

| Part | Class | Evidence |
|---|---|---|
| Structural checks: placeholder text, field limits, keyword count, review-notes placement, speaker-label consistency, content past duration, truncation, completeness and seam gates | **deterministic** | `style_engine/lint.py` (`run_lint` and the `_check_*` helpers). **Off in prod** (`"validator": {"lint": "off"}`). |
| Semantic checks ("title accurately reflects content", facts against SST) | **inferred** | `house_style.yaml` → `phases.validator.semantic_checks`. |
| Overall pass/fail → pause and escalate | **deterministic** | Applies a rule to the flags: `_finalize_with_qa_gate` (`worker.py:1501`), `escalation.py`. |

### A5. `timestamp_output.md` (chapters)

Producer: `prompts/timestamp.md` with the `{{style:timestamp.output_contract}}` block.
Runs automatically when the content is 10 minutes or longer and an SRT exists
(`_should_run_timestamp_phase`). It gets the **full SRT** (up to 500,000 characters),
`analyst_output[:4000]`, SST and the timestamp pre-stage section. The output has
**no AirTable target** (gap G3); editors copy and paste it.

| Part | Class | Evidence |
|---|---|---|
| Duration / final end time | **deterministic** | `get_srt_duration`; pre-stage `final_end`. |
| Boundary candidates | **deterministic** | `pre_stage._extract_boundary_candidates`: `>>`, bold labels and music cues, capped at 60 and evenly subsampled. |
| Max chapter count | **deterministic** | `chapter_max_by_duration` in `house_style.yaml`. |
| First chapter (0:00 "Episode intro") | **deterministic** | `snap_chapters` step 3, from `phases.timestamp.first_chapter`. |
| Choosing which candidates become chapters | **split → mostly inferred today** | Picking the topic shifts is inferred. Snapping a chosen point to the next clean cue is deterministic, and this is comparison-note point 5. For programs with fixed segment structure (music bumpers between stories), the music-cue candidates may be the chapter boundaries outright. That should be tested against real SRTs before relying on it. |
| Chapter titles | **inferred** | Sentence case and the 2–6 word length are deterministic checks (`chapter_name`, `to_down_style`). |
| Media Manager table (H:MM:SS.000 / .999, no gaps, last end = SRT end) | **deterministic** | `timecodes.emit_media_manager_table`. **Today the model does this math** because the slim profile only applies under `post: enforce`. |
| YouTube list (`M:SS Title`) | **deterministic** | `timecodes.emit_youtube_list`, same caveat. |

### A6. Other files and values the job produces

| Output | Class | Evidence |
|---|---|---|
| `manifest.json` | **deterministic** | `_create_manifest` (`worker.py:3652`). |
| Job metrics: `word_count`, `duration_minutes` | **deterministic** | `calculate_transcript_metrics`, `_resolve_duration_into_metrics` (prefers SRT). |
| `content_type` (full/short/clip) | **deterministic** | `_detect_content_type` (`worker.py:2185`): under 1.5 minutes → short; any SST string containing `"Clip"` → clip. *Side finding:* the clip test matches the substring in **any** SST string, so a title containing "Clip" would trip it. |
| Timestamp-phase trigger | **deterministic** | 10-minute threshold. |
| Chunk plan and SRT rewrites (interior `>>` split) | **deterministic** | `chunking.py`, `speaker_segmentation.py`. |
| Seam and completeness records | **deterministic** | `seam_coverage.py`, `completeness.py`. |
| Glossary additions from editorial feedback | **inferred** | `_extract_glossary_entries` (`worker.py:683`) is an LLM call. |
| Glossary mining from transcript-review edits | **deterministic** | `transcript_diff.py` (difflib, MIN_OCCURRENCES=2). |
| `copy_editor_output.md`, `copy_revision_vN.md` | **inferred** (human in the loop) | Claude Desktop / MCP only (`prompts/copy_editor.md`, `claude-desktop-project/EDITOR_AGENT_INSTRUCTIONS.md`, `save_revision`). Not part of the worker pipeline. |
| `keyword_report_vN.md` | **deterministic** | User-uploaded CSV wrapped in markdown (`jobs.py` keyword-report upload). |
| `recovery_analysis.md`, `investigation_report.md` | **no producer found** | Only labels exist (`mcp_server/server.py:97–98`, `web/src/utils/artifactLabels.ts`, export allowlists). Nothing in `api/` writes them. Candidates for removal. |
| Screen Grab attachment (AirTable) | **deterministic** | `screengrab_attacher.py`. Not caption-derived. Additive write outside the field map. |
| Transcription-stage SRT (WhisperX) | n/a | Audio→captions is out of scope for this map. |

---

## B. SST fields (`config/sst_field_map.json`)

### B1. Writable deliverables (`WRITABLE_FIELDS`, `mcp_server/server.py:171`)

| SST field | Key | Class | Producer today | Basis |
|---|---|---|---|---|
| Release Title | `title` | **inferred** | seo → Title | Copy. Limit, casing and the Here & Now template are deterministic guards, not the value. |
| Short Description | `short_description` | **inferred** | seo | Copy. 90-character truncation is deterministic. |
| Long Description | `long_description` | **inferred** | seo | Copy. 350-character limit. |
| General Keywords/Tags | `keywords` | **split** | seo; ambiguous between tiers and YouTube tags (G1) | Direct proper nouns, locations and branded terms are deterministic. Implied terms and ranking are inferred. The 15–20 count (5–10 for shorts) is deterministic. |
| Hashtags | `hashtags` | **split** | seo → Social Media Hashtags | Branded and `#Shorts` deterministic; topical inferred. |
| Social Media Description | `social_description` | **inferred** (if ever produced) | none; **human_authoritative** hazard (G2) | Read as truth for speaker names (`_extract_speakers_from_sst`, analyst and formatter prompts). Should not get a generator on this map. |
| Social Media Tags | `social_tags` | **split** (if produced) | none | Could be derived deterministically from keywords and hashtags once G1 is settled. |
| Facebook Description | `facebook_description` | **inferred** (if produced) | none | Copy. |

### B2. Read-only context fields

Read by `_extract_sst_fields` (`mcp_server/server.py:436`) and by
`SST_CONTEXT_FIELD_MAP` / `SST_PROJECT_FIELD_MAP` (`worker.py:75–102`). The pipeline
only reads these, so the question is whether they could be filled in when there is
no AirTable record.

| Field | Class if AirTable is absent | Basis |
|---|---|---|
| Media ID | **deterministic** | `utils.extract_media_id` from the caption filename. |
| Batch-Episode | **not derivable** | Human working title. The worker falls back to it for `title`. |
| Project → Project Name (program) | **deterministic** | Media ID prefix → show (`mmingest/media_id_prefixes.yaml`). |
| Project → Notes, Project Description | **not derivable** | Human series context. |
| Host, Presenter | **split** | Human-entered. When empty, regex-extracted from Project Notes and Social Media Description (`_extract_speakers_from_sst`). Not derivable from captions alone. |
| SD Character Count, LD Character Count | **deterministic** | `len()` of the descriptions. The names suggest AirTable formula fields; not confirmed by a schema read. |
| Episode Special Thanks | **not derivable** | Human-entered credits. |

---

## C. Summary table

| Output | Class |
|---|---|
| Transcript header, metrics, duration, content type, manifest | deterministic |
| Formatted transcript body (turns, substitutions, glossary, filler, layout) | deterministic |
| Speaker names on turns | split (named captions / SST: det.; anonymous `>>`: inferred) |
| Punctuation and garble repair (live captions) | inferred |
| Content-loss checks (completeness, seam, comparison-note checks) | deterministic |
| Chapter boundaries | split (candidates, snap, cap, first chapter: det.; topic-shift pick: inferred) |
| Chapter titles | inferred |
| Chapter formats (Media Manager, YouTube) | deterministic (code exists; off in prod) |
| Release Title / Short / Long / Facebook Description | inferred (limits, casing, voice scans deterministic) |
| General Keywords/Tags, YouTube tags, Hashtags, Social Media Tags | split |
| Analyst summary, themes, editorial opportunities, quotes selection | inferred |
| Analyst speaker table / structure / review items | split |
| Validator structural lint | deterministic (code exists; off in prod) |
| Validator semantic checks | inferred |
| SEO quality score, platform recs, alt text | inferred |
| Glossary from editor feedback | inferred; from review diffs: deterministic |
| Media ID, program, character counts | deterministic |
| Batch-Episode, Project Notes, Special Thanks, Host/Presenter | human input (not derivable) |
| Social Media Description | human-authoritative; no generator |

---

## Open points for downstream tickets

- **SCC parser** is a prerequisite for "SRT and SCC are canonical". Today there is
  none (finding 3).
- **The style engine's enforce tiers are off in prod.** Before a new generator
  rebuilds chapter rendering, casing or limit truncation, check whether
  `style_engine` can simply be its implementation. The code and tests exist.
- **Chapter snapping rule.** `snap_chapters` does not yet do "next clean cue after
  the chosen point". That is the concrete change behind comparison-note point 5.
- **Filler word list.** No deterministic filler list exists. Filler removal
  happens only inside the formatter prompt.
- **G1 (one keyword list or two)** decides whether YouTube tags and the keyword
  tiers become one split item or two.
- **Dead artifact labels** (`recovery_analysis.md`, `investigation_report.md`) have
  no producer.
