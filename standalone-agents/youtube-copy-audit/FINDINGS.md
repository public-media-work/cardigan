# POC Findings Log

Fill this in as you run the POC. These findings gate the full-feature build
(`standalone-agents/youtube-copy-audit/FEATURE.md`) — be specific and honest about
friction.

---

## Phase A.5 — timestamps-only slice (2026-09-02)

A narrower slice than the copy audit — chapter timestamps only — is being built as
a section of the pbswi `/audit-youtube-channels` skill plus a new
`/youtube-chapters` sibling. It exercises B4's write-back path in production
**before** B4 is built. Findings below come from that work; sections it cannot
speak to are marked so, rather than left blank.

**Why this slice is cheap:** chapter generation derives purely from the SRT and
needs **no Media-ID / SST join**. The join is Phase B's stated #1 risk, so this
slice sidesteps it entirely while still exercising caption fetch → Cardigan →
guarded write-back.

### Detection census — brand channel, full back catalogue

Run 2026-09-02, `chapter_audit.py --max-videos 6000`, ~190 quota units, one pass.

| Metric | Count |
|---|---|
| Uploads scanned | 4,739 |
| Long-form (10+ min, so Cardigan-eligible) | 1,861 |
| Too short to chapter | 2,878 |
| **No chapters at all** | **1,798** (96.6% of eligible) |
| **Timestamps present that do NOT render** | **17** |
| Already good | 46 |

Combined views on long-form videos with no chapters: **8.48M**.

### The `broken` bucket is the highest-value finding

**16 of the 17** broken videos fail for exactly one reason: the first marker is not
`0:00`. Someone wrote real chapters and started them at the first content beat
(0:29, 1:06, 1:31, 1:54, 3:27 …) instead of the cold open, so the block does not
meet YouTube's requirements and **the creator-defined chapters are not used**.
Combined views on those 16: **1.14M**.

**Important qualifier — do not overstate this.** YouTube also generates *automatic*
chapters, and that is ON by default for new uploads (opt-out per video or in Studio
upload defaults). So these videos are not necessarily chapter-less; they are most
likely showing YouTube's algorithmic guesses **instead of** the titles PBS Wisconsin
wrote. The value of the fix is **editorial control** — our chapter names, our
framing — not "making chapters appear." Anyone citing this finding should say it
that way.

Not yet verified: whether these specific videos display auto-chapters today. That
needs a browser check on the live watch pages, not an API call — `videos.list`
exposes nothing about automatic chapters.

The fix needs **no transcript, no Cardigan job, and no LLM** — only a prepended
`0:00 <title>` line. That is a one-line change per video against already-approved
editorial copy, and it is a far better first live-write target than generation.
Recommend it as the first exercise of the write path.

This also validates building the detector against YouTube's **rendering rules**
rather than "are there timestamps?" — a presence check would have scored all 17 as
fine and missed every one.

### Media-ID ↔ video mapping

**This slice cannot speak to it, by construction** — chapter generation needs no SST
join, so it never exercises the mapping. Do not read the blank as "no problems
found." Phase B's #1 risk remains open and unmeasured.

### Quota reality

Detection is far cheaper than the plan assumed: a **full 4,739-video census cost
~190 units**, so gap detection can run weekly at no meaningful cost. The expense is
entirely in per-video transcripts (`captions.download` ~200 + `captions.list` ~50),
which caps generation near 30–40 videos/day against the shared 10,000 budget.

Practical consequence: **detection and generation should be scheduled differently.**
Sweep everything weekly; work the generation queue down slowly.

### Still open (Stage 2 not yet built)

- **Caption quality** — manual vs auto track availability per show. Expect trouble:
  Cardigan's `pre_stage.py` builds boundary candidates from speaker tags and music
  cues, which an ASR transcript has neither of. Untested.
- **Pipeline fit / limits & style collisions / time & value** — no Cardigan job has
  been run from a YouTube-sourced transcript yet.

### Upstream bugs found while reading Cardigan

1. **`include_timestamps` is dead code.** Read at `api/services/worker.py:2134-2135`,
   never written — no API parameter (`upload_transcripts` takes only `files`), no DB
   column, no form field. It is always `False`, so the **only** reachable trigger for
   the timestamp phase is: SRT exists AND not a Short AND duration ≥ 10 min. Either
   wire it through or delete it and document the 10-minute rule as the sole trigger.
2. **`submit_and_wait.py:36` omits `timestamp_output.md`** from `OUTPUT_FILES`. Even
   a fully-executed POC would never have retrieved a chapter list.
3. **No JSON chapter emitter.** `timestamp_output.md` is markdown only, so consumers
   must parse the `## YouTube Format` heading. A `timestamp_output.json` beside
   `emit_youtube_list` (`api/services/style_engine/timecodes.py:210`) would be ~10
   lines plus allowlist entries in `api/routers/jobs.py` and `api/routers/export.py`.

---

## Videos processed

| Date | Video ID | Title | Media ID derived? (where) | SST linked? | Captions (manual/auto/none) | Job | Written back? |
|------|----------|-------|---------------------------|-------------|------------------------------|-----|----------------|
| 2026-09-02 | `qJ1eaJL-_BA` | June 21, 2024 \| Here & Now | n/a — repair path needs none | n/a | n/a — no transcript used | none | **yes, committed** |

**First live write of this feature — the 0:00 repair path, not generation.** One line
(`0:00 Episode intro`) prepended above an existing `1:10` marker; every other line
byte-identical. Verified after the write: detector re-reports the video as `ok`
(renders=True, 7 markers), `categoryId` and `defaultLanguage` preserved, mutation-log
record present.

**Chapter bar confirmed rendering on the live watch page** (human visual check,
2026-09-02). The full loop is proven end to end: detect → plan → guarded write →
re-detect → visible chapters. Note that the API cannot verify this last step —
`videos.list` exposes nothing about whether chapters render, so a human or browser
check remains the only confirmation.

### Batch — 15 more repairs, 2026-09-02

All 15 remaining missing-`0:00` videos written in one run: 14 Wisconsin Hometown
Stories, Jerry Apps, Wisconsin Lighthouses. **15/15 committed, 0 failures**, ~750
quota units. Re-verified afterwards: all 15 bucket `ok`, and an integrity check
confirmed **exactly one line added and none lost** on every video.

Remaining `broken` on the channel: 1 (Mile of Music — a 1-second segment between
`0:07:55` and `0:07:56`, a real editorial call the tool correctly refuses).

### GOTCHA — the key-path read lags a write

Verifying immediately after a write produced a **false failure**: one video
(`ns_7nlxi9f4`) reported `committed`, but a `videos.list` read seconds later still
returned the OLD description, so the checker flagged it as unchanged and still
`broken`. A re-read a few minutes on showed the write had landed correctly all along
(one mutation-log entry, correct content).

**Do not verify a write with an immediate key-path read.** The danger is not the
false alarm itself — it is the reflex it invites: re-writing (harmless here, but
double-inserting in a less idempotent tool) or rolling back a write that succeeded.
Wait, or verify against the `videos.update` response, or accept eventual consistency
and re-check on the next scheduled sweep.

### Editorial follow-up — the doubled intro

On **8 of the 15**, the added line now sits directly above the show's own opening
chapter, reading:

```
0:00 Episode intro
1:34 Intro
```

Both are defensible (a cold open, then the titles/introduction proper), but the
wording is redundant. Nobody has watched 0:00–1:34 on those episodes to say what it
actually is — cold open, underwriter credits, or teaser. `Episode intro` is
house-style-correct but content-blind.

This is a small instance of the general problem the review harness exists to solve:
**a generated or inserted chapter label is a claim about content nobody has
checked.**

**Resolved 2026-09-03.** A human who knows the series identified what was actually
there: a cold open followed by a titles sequence. Relabeled across all 8 to
`0:00 Cold Open` / `1:34 Opening Titles`. Verified: 8/8 still `ok`, exactly two lines
changed per video, nothing else touched.

Required a new capability — `retitle_chapters()` in `description_blocks.py` plus
`retitle.py` — because `plan_zero_marker_fix` sees a now-valid block and returns
`None`. Renames are exact-title-matched and must hit exactly one chapter; zero
matches (the description drifted) or several (ambiguous) both raise rather than edit
the wrong line.

**The transferable lesson for Stage 2:** the generic label was *syntactically*
correct and *editorially* wrong, and only a person who knew the show could tell.
Generation will produce this failure mode constantly and at a larger scale than a
one-word intro label. It is the strongest argument yet for building the review
harness before generating at volume.

Also learned: **casing is per-series, not per-channel.** Hometown Stories is
consistently Title Case, so the sentence-case `Episode intro` from `house-style` §8
clashed visibly. See `youtube-chapters/reference/observed-conventions.md`.

**This validates B4's write-back path in production before B4 exists.** The guarded
executor (`write_ops.py`) handled a real description edit on the brand channel with
the identity gate passing and `_merge_snippet` leaving unrelated fields alone.
`description_blocks.py` is the reference implementation B4 should port rather than
reinvent.

### Escaping — checked, no problem, but worth knowing

The `videos.update` **response** echoes `snippet.description` HTML-escaped (`Here
&amp; Now`) even though the value sent contained a plain `&`. A re-read via
`videos.list` confirms **storage is unescaped** — `&` stays `&`. So read-modify-write
does **not** double-escape. Do not "fix" the response's entities by unescaping before
a write; that would corrupt genuinely-escaped copy. Verify against a re-read, never
against the write response.

### Rollback

`videos.update` responses and the mutation log record the **new** value only, so the
log alone cannot restore a description. The repair tool keeps the original in its
planning output; stage an explicit rollback op before any batch. Worth building into
the tooling if this runs at volume.

## Media-ID ↔ video mapping

_The #1 question. How often was the Media ID derivable, from where, and how
often was it wrong? Does the full build need an SST YouTube-URL field or a
human-confirmed match step?_

-

## Caption quality

_Manual vs auto track availability per show; transcript cleanliness; videos
with no captions._

-

## Pipeline fit

_Did the 4 phases handle caption-style transcripts (no speaker labels)? Was the
SEO output sufficient for the audit, or was the raw transcript needed?_

-

## Limits & style collisions

_Where the 80/90/350 house limits and YouTube's 100/5000/500 pulled apart;
boilerplate handling in descriptions._

-

## Quota reality

_Actual units per cycle; any contention with pbswi skills._

-

## Time & value

_Wall-clock per video; which steps needed the human; which felt automatable._

-

## Verdict for the full build

_Go / no-go / go-with-changes, and what Phase B should do differently from the
plan._

-
