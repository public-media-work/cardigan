# YouTube Copy Audit — Proof of Concept

Validates the end-to-end loop for a planned Cardigan feature: **ingest a video from
the PBS Wisconsin YouTube channel, run its captions through the Cardigan pipeline,
compare YouTube + Airtable SST + generated copy in a "copy audit report," and write
approved metadata back to the live YouTube video.**

This is a standalone agent kit (no Cardigan backend changes). It borrows the pbswi
workspace's existing YouTube skills read-only at runtime — the OAuth management
token and the guarded write executor (`youtube-post/scripts/write_ops.py`) — and
talks to the production Cardigan API for processing. The full-feature design this
POC gates lives in `standalone-agents/youtube-copy-audit/FEATURE.md`.

## Files

| File | Purpose |
|------|---------|
| `PROMPT.md` | Agent operating instructions — the POC loop + guardrails |
| `fetch_video.py` | List channel uploads; fetch a video's snippet + caption transcript |
| `submit_and_wait.py` | Submit the transcript to Cardigan, poll the job, pull outputs + SST snapshot |
| `writeback.py` | Thin wrapper over pbswi `write_ops.py` — dry-run default, triple-locked live writes |
| `templates/COPY_AUDIT_TEMPLATE.md` | The three-way copy audit report template |
| `reference/write-back-safety.md` | Triple lock, quota budget, channel IDs, char limits |
| `FINDINGS.md` | Pain-point log — fill in as you run the POC; gates the full build |

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install requests google-api-python-client google-auth google-auth-oauthlib

# Where the pbswi workspace lives (for the OAuth token + write executor)
export PBSWI_ROOT="$HOME/Developer/pbswi"                # or the SSD path
export YOUTUBE_TOKEN_PATH="$PBSWI_ROOT/station-analytics/credentials/work/token.json"

# Cardigan API (production is the default per CLAUDE.md)
export CARDIGAN_API_URL="http://cardigan01:8100"
# export CARDIGAN_API_KEY=...   # only if the deployment has auth enabled
```

Optional fallback path: `yt-dlp` on PATH (used only when the captions API has no
downloadable track).

> **Cross-repo coupling (POC-only).** Two of the three scripts — `fetch_video.py`
> (OAuth token) and `writeback.py` (OAuth token + the write executor) — reach into
> pbswi's *internal* layout at runtime:
> `$PBSWI_ROOT/.claude/skills/content/youtube-post/scripts/write_ops.py` and
> `$PBSWI_ROOT/station-analytics/credentials/work/token.json`. (`submit_and_wait.py`
> only talks to the Cardigan API and has no pbswi dependency.) This is pinned to
> pbswi's **current** internal paths: a reorg over there (moving the youtube-post skill
> or the credentials dir) silently breaks this kit. It fails loud with a helpful message
> if the path is absent, which is acceptable for a POC. **At graduation**, depend on a
> stable published entrypoint (e.g. an installed console script) rather than the skill's
> script path.

## POC loop

```bash
# 1. Browse recent uploads, pick a video
python fetch_video.py list --max 25

# 2. Fetch snippet + captions (writes runs/<videoId>/snippet.json + transcript)
python fetch_video.py fetch <videoId>

# 3. Submit to Cardigan, wait for the 4-phase run, pull outputs + SST snapshot
python submit_and_wait.py runs/<videoId>

# 4. Fill templates/COPY_AUDIT_TEMPLATE.md -> runs/<videoId>/copy_audit_POC_<videoId>.md
#    (agent-assisted; human reviews/edits)

# 5. Write back — ALWAYS dry-run first; live only after explicit human approval
python writeback.py runs/<videoId>/op.json                    # dry-run preview
python writeback.py runs/<videoId>/op.json --live --confirm   # after approval only
```

First live write target: an **unlisted or low-stakes video** on the main channel.

## Success criteria

1. `fetch_video.py` produces a clean transcript for a video with manual captions
   AND one with only auto captions (fallback path).
2. The Cardigan job auto-links an SST record from the derived Media ID and all
   4 phases complete.
3. The copy audit report shows all three sources + a proposed combined version + diff.
4. A dry-run shows the exact `videos.update` payload; one confirmed write to an
   unlisted video renders correctly on YouTube and lands in the mutation log.
5. `FINDINGS.md` captures the pain points that decide the full build.
