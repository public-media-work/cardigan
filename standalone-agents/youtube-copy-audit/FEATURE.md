# YouTube Channel Ingest + Copy Audit + Metadata Write-Back

**Status:** Phase A (POC) shipped under `standalone-agents/youtube-copy-audit/`.
Phase B below is designed but **not built** — it is gated on the POC verdict in
`standalone-agents/youtube-copy-audit/FINDINGS.md`.

## Context

Cardigan ingests transcripts from files and generates SEO metadata, with the
Airtable SST as the comparison/write target. The PBS Wisconsin YouTube channel
(~4,600 videos on `UCtnFS8kY2D3VLaEtt_Jk2ZA`) carries its own titles,
descriptions and tags that are often stale or inconsistent with SST copy. This
feature adds a **YouTube ingest mode**: authorize Cardigan against the channel,
browse/search videos in a new dashboard tab, queue selected videos through the
existing 4-phase pipeline (pulling captions as the transcript), and produce a
**copy audit report** — a three-way comparison of YouTube metadata + SST
metadata + pipeline-generated copy with one proposed combined version. Approved
copy can then be **written back to the live YouTube video** after a diff +
final-approval step.

Decisions already made (2026-07-15):

1. POC processes jobs on **production cardigan01**, `YT_` filename prefix.
2. Dashboard write-back uses a **guarded REST endpoint** backed by a shared
   service that MCP tools also use (confirmation-token pattern preserves the
   human-approval guarantee of the propose→review→commit convention).
3. Server-side transcripts: **captions API preferred, yt-dlp fallback** when a
   video has no downloadable track.
4. First live write targets an **unlisted/low-stakes video**.

Prior art in the pbswi workspace (borrowed by the POC, ported in Phase B):
dual OAuth tokens (read-write scopes `youtube` + `youtube.force-ssl`), the
guarded write executor `youtube-post/scripts/write_ops.py` (dry-run default +
confirm + brand-channel identity gate + mutation log), yt-dlp caption fetch
(`here-now-highlights`), owner-authed uploads listing (`audit-youtube`), and
the bulk write-back strategy in pbswi `docs/youtube-shows-strategy.md`.

---

## Phase B — Full Build-Out

### B1. Credentials & deployment (first — blocks everything server-side)

New Docker secrets (add `.example` files in `secrets/`, `secrets:` blocks in
`docker-compose.yml` + `docker-compose.prod.yml` attached to `api`, `worker`,
`mcp`; add key names to `bootstrap_secrets()` in `api/services/secrets.py`;
update `scripts/sync-homelab-secrets.sh`):

| Secret | Contents |
|--------|----------|
| `youtube_oauth_client` | OAuth client secret JSON (GCP project 527567833673, or a separate Cardigan project if quota contention appears) |
| `youtube_oauth_token` | authorized_user JSON w/ refresh_token, scopes `youtube` + `youtube.force-ssl` (no `.upload` — Cardigan doesn't upload) |
| `youtube_api_key` | Data API key for public/no-auth reads |

- **`scripts/youtube_auth.py`** — bootstrap consent flow run on a workstation
  browser (InstalledAppFlow); hard identity gate: refuse to save a token unless
  `channels.list(mine=True)` == the PBS Wisconsin main channel (learning from
  pbswi issue #90, where the token authed as a personal channel). Headless
  re-auth story: if the refresh token is revoked, re-run locally and re-sync
  secrets.
- **Token refresh persistence:** persist rotated tokens to a small
  `youtube_oauth_state` DB table so container restarts don't depend on
  re-minting (google-auth refreshes in-memory only).
- Health check `GET /api/youtube/health` returning the authenticated channel
  identity — verifies plumbing before any feature code.

### B2. Backend service + staging + router

- **`api/services/youtube.py`** — `YouTubeService` shaped like
  `api/services/google_drive.py::GoogleDriveService` (lazy google-api imports,
  `get_secret()`):
  - `list_channel_videos()` — uploads-playlist walk, incremental via
    `publishedAfter`/etag
  - `get_video(video_id)`, `get_transcript(video_id)` — captions.list → manual
    `en` preferred → captions.download → text; yt-dlp fallback when no
    downloadable track (binary confined to the worker image)
  - `update_video_snippet()` — port `write_ops.py::build_request` semantics:
    merge ONLY title/description/tags, preserve categoryId, dry-run default,
    identity gate, audit rows to a new `youtube_mutations` table
- **Staging table:** alembic migration `022_add_youtube_videos_table.py` —
  `youtube_videos` mirroring `available_files` (`video_id`, `title`,
  `description`, `tags_json`, `published_at`, `duration`, `thumbnail_url`,
  `caption_status`, `media_id_guess`, `airtable_record_id`, `status`
  [new/queued/ignored/audited], `etag`, `last_synced_at`). Pydantic models in
  new `api/models/youtube.py`.
- **Scanner:** `api/services/youtube_scanner.py` mirroring
  `api/services/ingest_scanner.py`; start with a manual "Sync now" trigger only
  (protect quota), scheduler cadence later via the `ingest_scheduler.py`
  pattern.
- **Router `api/routers/youtube.py`:** `GET /api/youtube/videos`
  (search/filter/paginate the staging table), `POST /api/youtube/sync`,
  `POST /api/youtube/videos/{video_id}/queue` (fetch transcript → SST auto-link
  via `AirtableClient.search_sst_by_media_id()` on `media_id_guess` →
  `database.create_job()`; graceful "no captions yet — retry later" status when
  a fresh video lacks a track). Register alongside `ingest.py`.
- **Job model:** add `source: Optional[str]` (`upload|ingest|youtube`) and
  `youtube_video_id: Optional[str]` to `api/models/job.py` + migration
  `023_add_job_source_youtube.py`.

### B3. Pipeline: YouTube context + copy audit report

- `api/services/worker.py`: add `_fetch_youtube_context(job)` beside
  `_fetch_sst_context(job)` (stored snippet, refreshed live at job start),
  injected into phase prompts for `source == "youtube"` jobs.
- **Copy audit as a post-validator step** (like the optional `timestamp` phase
  — not a fifth core phase): new `prompts/copy_audit.md`, entry in
  `config/llm-config.json`, runs only for youtube-source jobs. Consumes
  `style_engine/phase_io.py::extract_seo_fields()` output + SST snapshot +
  YouTube snippet (+ transcript if the POC shows it's needed); writes
  `copy_audit_v{N}.md`, registered in `_create_manifest()` outputs, the
  outputs-endpoint allowlist in `api/routers/jobs.py`, and made retryable via
  `retry_single_phase()`. The report structure is the POC's
  `templates/COPY_AUDIT_TEMPLATE.md`.

### B4. Write-back: shared service + MCP trio + guarded REST + approval UI

- **Shared logic `api/services/youtube_edits.py`** (single source of truth for
  both MCP and REST):
  - `propose_youtube_edit()` — stage into
    `manifest.json["proposed_youtube_edits"]`; allowlist title/description/tags;
    enforce YouTube hard limits (100/5000/~500) + `config/house_style.yaml`
    limits where SST-bound
  - `review_proposed_youtube_edits()` — live re-fetch of the current snippet,
    unified per-field diff, style notes
  - `commit_youtube_edits()` — optimistic concurrency (abort if the live
    snippet changed since propose), `videos.update`, `youtube_mutations` audit
    row, optional mirror comment on the SST record (like
    `handle_commit_sst_edits`)
- **MCP:** three new tools in `mcp_server/server.py` mirroring the SST trio
  (`handle_propose_sst_edit` / `handle_review_proposed_edits` /
  `handle_commit_sst_edits`) as thin wrappers over the service.
- **REST:** `POST /api/jobs/{id}/youtube-writeback/propose` returns the diff +
  a one-time `confirmation_token` (hash of proposed payload + live etag);
  `POST .../commit` requires the token and rejects if stale. This preserves the
  CLAUDE.md human-confirmation guarantee outside MCP.
- **Web:**
  - New route `/youtube` in `web/src/App.tsx` + `pages/YouTubeVideos.tsx`
    mirroring `ReadyForWork.tsx`/`IngestPanel.tsx`: searchable table (title,
    thumbnail, published date, caption badge, SST-match badge), multi-select,
    Queue button, Sync now.
  - `JobDetail.tsx`: YouTube metadata panel beside the existing SST panel
    (youtube-source jobs only); `copy_audit` artifact registered in
    `OUTPUT_FILES`/`OUTPUT_TO_PHASE`; **WriteBackModal** — per-field
    old-vs-proposed diff, char counters, explicit "Write to live YouTube video"
    confirmation, success view shows the mutation-log entry. Cardigan's first
    approval UI — keep it single-purpose.

### B5. Rollout order

1. B1 credentials + `youtube_auth.py` + `GET /api/youtube/health`
2. B2 service + staging + router + job-model migration + read-only `/youtube`
   tab (immediate value)
3. B3 queue → transcript → 4-phase → copy-audit end-to-end
4. B4 MCP write-back trio first (lowest risk, matches existing convention)
5. B4 REST + JobDetail approval UI, gated on a few successful MCP-mediated
   writes

### Risks

- **Media-ID mapping** is the biggest unknown (POC question #1); mitigations:
  an SST field for YouTube URL/ID, or fuzzy title matching with human
  confirmation in the `/youtube` tab.
- Quota contention with pbswi skills in the shared GCP project
  (captions.download = ~200 units caps ~50 transcript pulls/day); separate
  project if needed.
- Two writers to one channel (Cardigan prod + pbswi `write_ops.py`): both keep
  mutation logs; the optimistic-concurrency re-fetch is the collision guard.
- yt-dlp fallback = maintenance surface; confine to the worker image, treat the
  captions API as primary.
- Only the main channel is writable — hard-code the identity gate in
  `YouTubeService`; refuse writes to any other channelId (Education stays
  read-only).

### Verification (Phase B)

`pytest` with new tests for the scanner, router and youtube_edits service
(mirror `tests/test_srt_duration_ingest.py` style); `GET /api/youtube/health`
returns the brand channel; end-to-end: sync → queue from the `/youtube` tab →
copy_audit artifact visible in JobDetail → propose/commit via MCP, then via the
WriteBackModal, against an unlisted video; verify the optimistic-concurrency
abort by editing the video in YouTube Studio between propose and commit.
