# SST field map — gaps

Companion to `config/sst_field_map.json`, which holds the mapping itself as data.
This file answers questions 3 and 4 of the "Map every deliverable to its AirTable
SST field" ticket, on the closed map [Wayfinder] Cardigan V5 (#328).

**Method note.** Everything below is derived from code — `WRITABLE_FIELDS` carries
real AirTable field ids, so the eight writable fields are confirmed. **No live
AirTable schema read was performed** (no AirTable MCP server or credential was
available in the session that produced this). Gap **G3** in particular needs a
schema read to close, and any *new* field is Lisa's call, not ours.

---

## G1 — Two producers compete for `General Keywords/Tags`

`seo.md` emits two separate 15–20 item lists:

1. The **Keyword Strategy** tiers — Primary, Secondary, Long-Tail, Location.
   `config/house_style.yaml` (line 330) says *"Aim for 15-20 total keywords across
   the SEO Keywords section above"*, which reads as the flattened union of those tiers.
2. A distinct **"YouTube Tags (15–20 recommended)"** block.

`house_style.yaml` sets `keywords: {count: {min: 15, max: 20}}` — one budget, and
both candidates match it, so the count can't disambiguate them.

**Nothing in the prompt or the config says which list lands in the field.** Whichever
one doesn't win has no AirTable target at all.

**Not resolved here** — this is a definition question and belongs to *Define the item*.
Flagging both candidates is the deliverable.

---

## G2 — `Social Media Description` is writable *and* read as ground truth

This is the sharpest finding, and it is a live hazard rather than a missing feature.

- It sits on the **write allowlist** (`fldntHlzk6PfIT5k2`), so `commit_sst_edits`
  can write it today.
- **No pipeline phase produces it** — nothing can fill it.
- Both `analyst.md` and `formatter.md` read it as the **authoritative** source for
  who appears in an episode:

  > *"The `Social Media Description` field often lists the specific reporters/hosts
  > for each episode … These are authoritative sources for speaker identification."*
  > — `prompts/analyst.md`

  > *"Cross-reference ALL speaker names against SST data — The `Social Media
  > Description` field often lists the specific reporters/hosts … These are
  > authoritative; caption text is not."*
  > — `prompts/formatter.md`

So a write to this field doesn't just overwrite a producer's copy — it corrupts the
input the pipeline uses to identify speakers on **every subsequent run** for that
episode. The damage is silent and compounding.

**Recommendation:** reclassify to `human_authoritative` and drop it from
`WRITABLE_FIELDS` until a producer exists and the speaker-identification role is
moved somewhere else. It is currently the only allowlisted field the pipeline also
consumes as truth.

---

## G3 — The platform-notes composite does not exist in code

The map's stage 5 says publish *"also writes transcript link + timestamps to platform
notes."* Auditing that:

- The strings `platform note`, `platform_notes`, and `Platform Notes` appear
  **nowhere** in the repository.
- `WRITABLE_FIELDS` has eight entries; none is a notes field.
- `_extract_sst_fields` reads fifteen fields; none is a notes field.
- `timestamp.md` states chapters are *"copy-paste[d] into publishing platforms"* by
  editors — i.e. the handoff is manual today, by design.

**So the question as posed — "does writing one clobber the other?" — has no answer
yet, because neither is written.** There is no composite to clobber. The concern is
real but premature: it becomes real the moment a single field takes both values.

**To close this, someone needs to:**
1. Read the live SST schema and confirm whether a suitable notes field exists
   (name, type, and whether it is long-text or single-line).
2. If it exists, decide the format — the two values need a stable, parseable
   separator so a rewrite of one preserves the other. Append-blind writes will
   clobber.
3. If it does **not** exist, creating it is AirTable-native configuration —
   **Lisa's domain.** Raise it as a suggestion for her review, don't create it.

Until then, treat transcript link and chapters as unmapped (they are recorded that
way in the JSON).

---

## G4 — Question 4, stated directly

### Allowlisted fields with no producer (3 of 8)

| Field | Consequence |
|---|---|
| `Social Media Description` | Writable, unfillable, **and read as truth** — see G2 |
| `Social Media Tags` | Writable, unfillable. Inert but misleading |
| `Facebook Description` | Writable, unfillable. Inert but misleading |

**Five of eight allowlisted fields actually work.** The publishable surface is
Release Title, Short/Long Description, General Keywords/Tags, and Hashtags.

### Producers that cannot be published today

| Producer | Phase | Why it can't publish |
|---|---|---|
| Formatted transcript | `formatter` | No target field (G3) |
| Chapter timestamps | `timestamp` | No target field (G3) |
| YouTube Tags | `seo` | Loses the G1 contest, or wins it and displaces the tiers |
| Quality Score | `seo` | Correctly has no target — internal QA, not a deliverable |

`formatter` is the notable one: it is a **required** pipeline phase whose entire
output has nowhere to go in AirTable.

---

## What this means for the item model

- **5** deliverables are publishable end-to-end today.
- **1** is a hazard to be removed from the allowlist (G2).
- **2** are inert allowlist entries (`Social Media Tags`, `Facebook Description`).
- **3** producers are stranded without a target (transcript, chapters, YouTube tags).
- **1** producer (`Quality Score`) is correctly context-only.

The item primitive — *proposed value, current AirTable value, status* — needs a
fourth axis these gaps expose: **whether the item can be published at all.** An item
with no target field, or one whose field is human-authoritative, is not merely
"not yet approved" — it is unpublishable, and the approval surface should say so
rather than offering an approve button that silently does nothing.
