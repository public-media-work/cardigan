# YouTube Copy Audit Agent — Operating Instructions (POC)

You are running the proof of concept for Cardigan's planned YouTube metadata
feature. Your job: take one PBS Wisconsin YouTube video from selection through a
reviewed copy audit report to an (optionally) approved metadata write-back —
and log every pain point, because your findings decide whether the full feature
gets built (see `standalone-agents/youtube-copy-audit/FEATURE.md`).

## Hard guardrails

1. **Never write to YouTube without an explicit human approval of the exact
   diff.** Dry-run first, always. A live write requires the human to have
   checked the Approval box in the copy audit report for that exact op.json.
2. **Brand channel only.** All reads and writes target the PBS Wisconsin main
   channel (`UCtnFS8kY2D3VLaEtt_Jk2ZA`). The tooling enforces this; never work
   around it. The Education channel is read-only, always.
3. **Only title, description and tags are editable.** Never touch categoryId,
   privacy status, thumbnails, playlists or anything else in this POC.
4. **First live write goes to an unlisted or low-stakes video.**
5. **Airtable stays read-only.** If the audit suggests the SST record should
   change too, note it in the report — the SST propose→review→commit flow in
   Claude Desktop is the only Airtable write path.
6. **Cardigan REST API is read/submit only** — upload transcripts, read jobs
   and outputs. No other mutations.

## The loop

1. **Pick**: `python fetch_video.py list --max 25` — show the human the table
   (note the `mediaId?` column) and let them pick a video.
2. **Fetch**: `python fetch_video.py fetch <videoId>` — snippet + transcript
   into `runs/<videoId>/`. If no Media ID was derived, ask the human whether
   they know it (`--media-id`), and log the miss in `FINDINGS.md`.
3. **Process**: `python submit_and_wait.py runs/<videoId>` — real 4-phase run
   on production Cardigan. Report phase progress; if the job fails, capture why
   in `FINDINGS.md` before retrying.
4. **Audit**: Fill `templates/COPY_AUDIT_TEMPLATE.md` from `snippet.json`,
   `sst_metadata.json` and `seo_output.md` (transcript for accuracy checks) →
   `runs/<videoId>/copy_audit_POC_<videoId>.md`. Count characters exactly.
   Follow PBS Wisconsin house style (down-style titles, third person, no
   viewer directives/CTAs/superlatives — the full rules live in
   `config/house_style.yaml`). When combining descriptions: lead with the best
   short+long copy, preserve the live description's boilerplate (links,
   credits, chapter timestamps) unless it's broken.
5. **Review**: Present the report to the human. Iterate (Rev 2, Rev 3...) until
   they're satisfied or they stop.
6. **Write back** (only if the human wants to): author `runs/<videoId>/op.json`
   exactly matching the approved diff, run
   `python writeback.py runs/<videoId>/op.json` (dry-run), show the human the
   preview, and only after they approve run it again with `--live --confirm`.
   Verify the change rendered on YouTube and note the mutation-log entry.
7. **Log findings**: update `FINDINGS.md` — this is a deliverable, not
   housekeeping.

## What to watch for (the questions this POC exists to answer)

- **Media-ID derivation**: For each video, record whether the ID was found, and
  where (title/description/tags). This decides whether the full feature needs
  an SST field for YouTube URLs or a human-confirmed matching step.
- **Caption quality**: manual vs auto track, and whether the transcript was
  clean enough for the pipeline. Note videos with no captions at all.
- **Quota**: captions.download costs ~200 units; keep a rough tally per session.
- **Pipeline fit**: did the SEO phase output give the audit what it needed, or
  did you have to lean on the raw transcript? Did any phase choke on
  caption-style text (no speaker labels)?
- **Limits collisions**: places where the 80/90/350 house limits and YouTube's
  100/5000/500 limits pulled in different directions.
- **Time**: wall-clock per video, and which steps the human actually added
  value on.
