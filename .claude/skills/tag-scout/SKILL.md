---
name: tag-scout
description: Generate candidate YouTube and Media Manager tags from an SRT or SCC caption file, using cardigan's own SEO agent rules. Use when the user hands over a caption or transcript file and wants keywords or tags without running the full four-phase pipeline. Triggers on "tags for this", "generate tags", "keywords for this episode", "tag scout", "what should I tag this".
---

# tag-scout

Cardigan's four-phase pipeline produces keywords as one field among many, after
analyst → formatter → seo → validator. This skill does the tag slice alone, from
a caption file, in one pass.

It is a **spike for v5's per-field deliverables.** It reads the same rules the
pipeline reads rather than copying them, so it stays honest as they change.

**This skill never writes to Airtable.** It prints candidates for a human to
paste or reject. SST writes only ever happen through the MCP
`propose → review → commit` workflow.

## Usage

```
/tag-scout <path-to-file.srt|.scc> [program name]
```

The program name is optional — the skill infers it from the media ID when the
filename follows the PBS Wisconsin grammar.

## Procedure

### 1. Extract the caption text

```bash
python3 .claude/skills/tag-scout/scripts/captions_to_text.py <file> --json
```

Dependency-free; works on SRT and SCC (detected by content, not extension).
Returns `format`, `duration_minutes`, `cue_count`, `live_captioning`,
`word_count`, and `text`.

Two things it handles that a naive read would not: SCC is a raw EIA-608 byte
stream needing parity masking and a non-ASCII character table, and
live-captioned SRTs repeat the whole rolling window in every cue, which would
otherwise inflate every term's frequency two or three times over.

If it errors, stop and report — do not fall back to reading the raw file
yourself. A hand-read SCC yields plausible-looking garbage, which is worse than
an error.

### 2. Identify the program

Take it from the argument if given. Otherwise take the first four characters of
the filename and look them up:

```bash
grep -A2 'prefix: "6HNP"' api/services/mmingest/media_id_prefixes.yaml
```

The prefix table's show names and `config/house_style.yaml`'s `programs:` keys
do not always match verbatim — the table says "Here and Now Packages" where the
style config says "Here & Now". Match on the obvious stem and **say which
program rules you applied**. If nothing matches, proceed with the cross-cutting
rules and say so; do not guess a program.

### 3. Load the rules

Read `.claude/skills/tag-scout/references/tag-rules.md` — the tag-relevant
extract of `prompts/seo.md`, `prompts/analyst.md`, and `config/house_style.yaml`.

Read the live counts from the YAML rather than trusting the extract's copy:

```bash
grep -A12 '^limits:' config/house_style.yaml
grep -A6 '^  "<Program Name>":' config/house_style.yaml
```

### 4. Generate candidates

Follow §2–§7 of the rules reference: the 15–20 composition (primary /
secondary / branded / long-tail / location), extraction heuristics, and any
program-specific rule. Short-form content takes the 5–10 override.

Label every candidate `direct` (spoken verbatim in the captions) or `implied`
(conceptual, not said). **Never invent search-volume, difficulty, or CPC
numbers** — you have no live keyword data.

If `live_captioning` is true, do not reconstruct proper nouns from garbled
caption text. Uncertain names go in a "needs verification" list, never in a
paste-ready line.

### 5. Report

Emit exactly this shape:

---

**Source:** `<filename>` · `<format>` · `<duration>` min · `<word_count>` words · captions: `<live | post-production>`
**Program:** `<resolved program>` — rules applied: `<which, or "cross-cutting only">`

**YouTube tags (paste-ready):**

`<comma-separated, priority-ordered, 15-20 or 5-10>`

**Media Manager tags (paste-ready):**

`<comma-separated topical core>`

**Evidence**

| Tag | Slice | Source | Relevance | Basis |
|---|---|---|---|---|
| … | primary/secondary/branded/long-tail/location | direct/implied | 1–10 | where it came from |

**Needs verification:** `<uncertain proper nouns, or "none">`
**Notes:** `<judgment calls, program-rule conflicts, anything the editor should overrule>`

---

Then stop. Do not propose an SST edit, and do not offer to write the tags
anywhere.

## Output rules

**Paste-ready lines are plain paragraphs — never in a code fence or backticks.**
The value goes into Airtable's richText `General Keywords/Tags` field, where a
pasted fence corrupts the first and last keyword *and renders invisibly*. The
editor sees a clean field with wrong tags. That's issue #380; do not reintroduce
it. (The fenced examples above are this document describing the format — the
actual output is unfenced.)

## What this skill is not

Cardigan writes **one** keyword value today, to `General Keywords/Tags`. There
is no separate Media Manager tag field in the write path. The two-list split is
a **proposal for v5**, grounded in `seo.md`'s platform guidance but ultimately
an editorial judgment: YouTube takes the long-tail and branded search terms;
Media Manager takes the topical core, since the PBS app weights category
accuracy over keyword breadth. Say so in the Notes when the two lists diverge
meaningfully, and treat the boundary as the open question this spike exists to
answer.

Also out of scope: titles, descriptions, chapter timestamps, and social copy.
Those are still the pipeline's job.
