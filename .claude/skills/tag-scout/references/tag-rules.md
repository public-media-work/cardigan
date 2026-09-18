# Tag and keyword rules

The tag-relevant slice of cardigan's agent definitions, extracted so this skill
can run without loading the full 336-line `prompts/seo.md` and 254-line
`prompts/analyst.md`.

**Provenance.** Every rule below traces to a file in this repo. When they
disagree, the source file wins and this extract is stale — say so rather than
following the stale copy.

| Rule area | Source |
|---|---|
| Tag composition, sourcing discipline, platform notes | `prompts/seo.md` |
| Keyword extraction heuristics, live-caption handling | `prompts/analyst.md` |
| Counts, program rules, forbidden language | `config/house_style.yaml` |
| Destination field and its ID | `mcp_server/server.py` `WRITABLE_FIELDS` |

---

## 1. Counts

Read these from `config/house_style.yaml` at run time rather than trusting the
numbers here — the YAML is the authored source and these are a convenience copy.

- `limits.fields.keywords.count` — **15–20** keywords, standard.
- `limits.content_type_overrides.short.keywords.count` — **5–10** for short-form.

Digital Shorts and anything under roughly three minutes takes the short
override. When the count is ambiguous, say which you applied.

## 2. Composition of the 15–20 (`prompts/seo.md`, "Tag Strategy")

| Slice | Count | What it is |
|---|---|---|
| Primary | 3–5 | Exact-match core topic terms |
| Secondary | 5–7 | Variations and near-synonyms |
| Branded | 2–3 | `PBS Wisconsin`, the program name |
| Long-tail | 3–5 | Question-based and niche phrasings |
| Location | 2–3 | Wisconsin cities, regions, landmarks, institutions |

**Order matters.** Highest-priority tags go first; `seo.md` notes the first
two or three carry the most weight on YouTube.

## 3. Sourcing discipline — the rule that matters most

Every candidate is labeled in the evidence table:

- **`direct`** — the exact term, name, or phrase is spoken in the captions.
- **`implied`** — a conceptual or search-intent term not said verbatim.

**Never invent search-volume, difficulty, CPC, or trend numbers.** `seo.md`
is explicit: you have no live keyword data. Volume figures belong only in the
SEMRush section of a full SEO report, and only when the user supplied the data.
A relevance score is your own editorial judgment and is fine; a search volume
is a fabrication.

## 4. Extraction heuristics (`prompts/analyst.md`, "Keyword Research")

1. Pull proper nouns — places, people, organizations.
2. Identify domain-specific terminology.
3. Note Wisconsin-specific locations, landmarks, programs.
4. Consider search intent: what would someone Google to reach this content?
5. Expand semantically — related terms, synonyms, variations.

## 5. Live-captioned input

`analyst.md` flags live/real-time captioning by `>>` speaker markers, literal
stutters and false starts, captioner self-corrections, and phonetically garbled
proper nouns. `captions_to_text.py --json` reports this as `live_captioning`.

When it is true:

- **Never reconstruct a proper noun from garbled caption text.** This is the
  single worst failure mode here — it produces confident, wrong tags that get
  pasted into a live record. If a name is uncertain, put it in a separate
  "needs verification" list, not in the paste-ready line.
- Treat frequency as unreliable; the captioner's repetitions are not emphasis.

## 6. Program-specific rules (`config/house_style.yaml`, `programs:`)

| Program | Tag-relevant rule |
|---|---|
| Here & Now | Electeds carry party and location; executive titles capitalized |
| University Place | `require_series_keyword: true` — the series name must appear |
| Wisconsin Life | `location_tags: important` — weight location higher |
| The Look Back | Must name hosts, institutions, historians |
| Digital Shorts | 6–8 word titles; short-form keyword count applies |
| Garden Wanderings | Botanical accuracy critical; include location and plant species |

Programs without an entry fall back to the cross-cutting rules.

## 7. Forbidden language

`house_style.yaml` `voice.forbidden_phrases` bans viewer directives — "watch
as", "watch how", "see how", "follow", "discover". These are aimed at
descriptions, but a long-tail tag phrased as a directive ("discover Wisconsin
state parks") violates the same rule. Phrase long-tails as noun phrases or
questions instead.

## 8. Output format — the #380 rule

Paste-ready lines are emitted as **plain paragraphs on their own line, never
inside a code fence or backticks.**

This is not cosmetic. The value gets pasted verbatim into the Airtable
`General Keywords/Tags` field (`fldjdPEXZyvx3rc6Y`), which is richText. A
pasted fence corrupts the first and last keyword and renders invisibly, so the
damage is silent — the editor sees a clean field and the tags are wrong. See
issue #380.

## 9. Destination fields

Cardigan today writes one keyword value, to `General Keywords/Tags`, via the
`propose_sst_edit → review_proposed_edits → commit_sst_edits` MCP workflow.
**There is no separate Media Manager tag field in the current write path.**

The two-list split this skill produces is therefore a proposal for v5's
per-field deliverables, not a rule lifted from an existing contract. It is
grounded in `seo.md`'s platform guidance — YouTube rewards long-tail and
branded search terms; the PBS app "focus[es] on category accuracy" with
"keywords less important than on YouTube" — but the boundary is an editorial
judgment call. Label it as such in the output.

**This skill never writes to Airtable.** It prints candidates for a human to
paste or reject.
