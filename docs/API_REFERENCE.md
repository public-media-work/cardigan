# Editorial Assistant API Reference

## Overview
- Base URL: `http://cardigan01:8100` (production/homelab — default for real work) · `http://localhost:8100` (local dev only). Target the name `cardigan01`, not a tailnet IP. See `CLAUDE.md` › "Deployment Environments".
- API prefix: `/api`
- WebSocket: `/api/ws/jobs`
- Auth: none on the homelab box today (tailnet/LAN-only); when `CARDIGAN_API_KEY` is set, send it as an `X-API-Key` header.
- Response format: JSON unless noted
- Errors: FastAPI default (`{"detail": "..."}`) unless noted

## Core Models (summary)
- JobStatus: `pending`, `in_progress`, `completed`, `failed`, `cancelled`, `paused`, `investigating`
- PhaseStatus: `pending`, `in_progress`, `completed`, `failed`, `skipped`

### Job (high-level fields)
- `id` (int), `project_path` (str), `transcript_file` (str), `project_name` (str)
- `status` (JobStatus), `priority` (int)
- `queued_at`, `started_at`, `completed_at` (timestamps)
- `estimated_cost`, `actual_cost` (float)
- `current_phase` (str), `phases` (array of phase objects)
- `retry_count`, `max_retries` (int)
- `error_message`, `error_timestamp`
- Airtable: `airtable_record_id`, `airtable_url`, `media_id`
- Transcript metrics: `duration_minutes`, `word_count`
- `outputs` (manifest-derived output file paths)

### JobCreate
- `project_name` (required)
- `transcript_file` (required, relative to `transcripts/`)
- `project_path` (optional)
- `priority` (optional, default 0)

### JobUpdate
- `status`, `priority`, `current_phase`, `error_message`
- `estimated_cost`, `actual_cost`, `manifest_path`, `logs_path`, `last_heartbeat`
- Airtable: `airtable_record_id`, `airtable_url`, `media_id`
- Transcript metrics: `duration_minutes`, `word_count`
- `phases` (full replace) or `phase_update` (single phase update)

## Health
### GET `/`
- Returns `{ "status": "ok", "version": "3.0.0-dev" }`

### GET `/api/system/health`
- Returns queue stats and LLM status:
  - `queue.pending`, `queue.in_progress`
  - `llm.active_backend`, `llm.active_model`, `llm.active_preset`, `llm.primary_backend`, `llm.configured_preset`, `llm.fallback_model`, `llm.phase_backends`, `llm.openrouter_presets`
  - `last_run` cost/tokens summary

## Queue
### GET `/api/queue/`
- Query params:
  - `status` (optional JobStatus)
  - `page` (default 1), `page_size` (default 50, max 100)
  - `search` (project path or transcript filename)
  - `sort` (`newest` or `oldest`)
- Returns paginated jobs:
  - `{ jobs, total, page, page_size, total_pages }`

### POST `/api/queue/`
- Body: `JobCreate`
- Query params: `force` (bool, default false)
- 201 returns `Job`
- 409 duplicate (unless `force=true`):
  - `detail.message`, `detail.existing_job_id`, `detail.existing_status`, `detail.action_required`, `detail.hint`

### DELETE `/api/queue/bulk`
- Query params: `statuses` (repeatable list of JobStatus)
- Safety: never deletes `pending` or `in_progress` even if requested
- Returns `{ deleted_count, message }`

### DELETE `/api/queue/{job_id}`
- Deletes a job record (204 on success)

### GET `/api/queue/next`
- Returns next pending job or 404

### GET `/api/queue/stats`
- Returns counts by status plus `total`

## Jobs
### GET `/api/jobs/{job_id}`
- Returns `Job` or 404

### PATCH `/api/jobs/{job_id}`
- Body: `JobUpdate`
- Returns updated `Job`

### POST `/api/jobs/{job_id}/pause`
- Allowed: `pending`, `in_progress`
- Returns updated `Job`

### POST `/api/jobs/{job_id}/resume`
- Allowed: `paused`
- Returns updated `Job`

### POST `/api/jobs/{job_id}/retry`
- Allowed: `failed`
- Resets status to `pending` and clears error
- Returns updated `Job`

### POST `/api/jobs/{job_id}/cancel`
- Allowed: `pending`, `in_progress`, `paused`
- Returns updated `Job`

### GET `/api/jobs/{job_id}/events`
- Returns list of `SessionEvent` records

### GET `/api/jobs/{job_id}/outputs/{filename}`
- Returns plain text (markdown/json)
- Allowed filenames:
  - `analyst_output.md`, `formatter_output.md`, `seo_output.md`, `manager_output.md`
  - `timestamp_output.md`, `copy_editor_output.md`, `recovery_analysis.md`, `manifest.json`
  - `copy_revision_v{n}.md`, `keyword_report_v{n}.md`
- 400 if filename is invalid; 404 if file not found

### GET `/api/jobs/{job_id}/sst-metadata`
- Fetches Airtable SST metadata for linked record
- 404 if job or record not linked
- 503 if Airtable not configured; 502 for Airtable fetch errors

### POST `/api/jobs/{job_id}/phases/{phase_name}/retry`
- `phase_name` can be phase (`analyst`, `formatter`, `seo`, `manager`, `timestamp`) or output key (`analysis`, `formatted_transcript`, `seo_metadata`, `qa_review`, `timestamp_report`)
- Returns `{ success, phase, message, cost?, tokens? }`
- Runs in background

## Upload
### POST `/api/upload/transcripts`
- Multipart: `files` (list of .txt/.srt)
- Constraints: max 20 files, max 50MB each
- Returns:
  - `{ uploaded, failed, files: [{ filename, success, job_id?, error? }] }`

## Config
### GET `/api/config/phase-backends`
- Returns `{ phase_backends, available_backends, available_phases }`

### PATCH `/api/config/phase-backends`
- Body: `{ phase_backends: { phase: backend } }`
- Valid phases: `analyst`, `formatter`, `seo`, `manager`, `copy_editor`

### GET `/api/config/routing`
- Returns tier routing configuration

### PATCH `/api/config/routing`
- Body: any of `duration_thresholds`, `phase_base_tiers`, `escalation`

### GET `/api/config/worker`
- Returns worker defaults `{ max_concurrent_jobs, poll_interval_seconds, heartbeat_interval_seconds }`

### PATCH `/api/config/worker`
- Updates worker defaults in `config/llm-config.json`
- Note: running workers must restart to pick up changes

## System Control
### GET `/api/system/status`
- Returns status for API, worker, watcher (running flag, pid, heartbeat age, container name)

### POST `/api/system/restart`
- Requests a restart of all running components (api + worker; dev watcher if present)
- Writes a single `restart_requested_at` signal; each component compares it to its own
  start time and self-restarts, and Docker's `restart: unless-stopped` brings it back
- Returns `{ requested_at, components, message }` — `components` lists what will actually cycle
- Mutating → requires `X-API-Key` when `CARDIGAN_API_KEY` is set

### POST `/api/system/watcher/heartbeat`
- Watcher liveness ping; body `{ started_at }` (the watcher's boot time)
- Returns `{ success, message, restart }` — `restart` is true when a restart request postdates `started_at`

## Ingest (Remote Server Monitoring)
### POST `/api/ingest/scan`
- Query params: `base_url` (default mmingest), `directories` (comma-separated)
- Returns scan summary

### GET `/api/ingest/status`
- Returns file counts by status/type

### GET `/api/ingest/screengrabs`
- Query params: `status` (`new`, `attached`, `no_match`, `ignored`), `limit`
- Returns list and totals

### POST `/api/ingest/screengrabs/{file_id}/attach`
- Attaches screengrab to SST record
- SAFETY: appends attachments, never replaces

### POST `/api/ingest/screengrabs/attach-all`
- Batch attach all pending screengrabs

### POST `/api/ingest/screengrabs/{file_id}/ignore`
### POST `/api/ingest/screengrabs/{file_id}/unignore`

## WebSocket
### WS `/api/ws/jobs`
- Server sends events:
  - `job_created`, `job_updated`, `job_completed`, `job_failed`
  - `stats_updated`
- Payload:
  - `{ "type": "...", "job": { ... } }` or `{ "type": "stats_updated", "stats": { ... } }`
- Client can send `ping` and receives `pong`
