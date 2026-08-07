# Agent Interface Guide

This guide is a single, high-signal entry point for agents that need to interface with the Editorial Assistant's API and MCP services.

> **Base URL — production vs local.** For real/production work, target the
> homelab-hosted container: **`http://cardigan01:8100`** (Tailscale MagicDNS name,
> CTID 103; LAN fallback `192.168.1.42:8100`). Use `http://localhost:8100` only
> for local development. The examples below use `localhost` for brevity —
> substitute the production base URL for anything other than local dev. See
> `CLAUDE.md` › "Deployment Environments" for the full rule.

## Quick Start (Minimal Integration)
1) Check health
- `GET http://localhost:8100/`
- `GET http://localhost:8100/api/system/health`

2) List queue
- `GET http://localhost:8100/api/queue/?status=pending&page=1&page_size=50`

3) Fetch job details and outputs
- `GET http://localhost:8100/api/jobs/{job_id}`
- `GET http://localhost:8100/api/jobs/{job_id}/outputs/manifest.json`
- `GET http://localhost:8100/api/jobs/{job_id}/outputs/analyst_output.md`

4) WebSocket for live updates
- Connect: `ws://localhost:8100/api/ws/jobs`
- Listen for `job_*` and `stats_updated` events

5) MCP (Cardigan) for copy editing
- Use MCP tools to list, load, and save project artifacts without touching the API directly.

## API Interface (HTTP)
Base URL: `http://cardigan01:8100` (production/homelab) · `http://localhost:8100` (local dev only)

### Health
- `GET /` -> `{ "status": "ok", "version": "3.0.0-dev" }`
- `GET /api/system/health` -> queue stats + LLM status

### Queue
- `GET /api/queue/` (filters: `status`, `page`, `page_size`, `search`, `sort`)
- `POST /api/queue/` (body: `JobCreate`, query: `force=true` to bypass duplicate detection)
- `DELETE /api/queue/bulk?statuses=completed&statuses=failed`
- `DELETE /api/queue/{job_id}`
- `GET /api/queue/next`
- `GET /api/queue/stats`

### Jobs
- `GET /api/jobs/{job_id}`
- `PATCH /api/jobs/{job_id}` (body: `JobUpdate`)
- `POST /api/jobs/{job_id}/pause`
- `POST /api/jobs/{job_id}/resume`
- `POST /api/jobs/{job_id}/retry`
- `POST /api/jobs/{job_id}/cancel`
- `GET /api/jobs/{job_id}/events`
- `GET /api/jobs/{job_id}/outputs/{filename}`
- `GET /api/jobs/{job_id}/sst-metadata`
- `POST /api/jobs/{job_id}/phases/{phase_name}/retry`

### Upload
- `POST /api/upload/transcripts` (multipart: `files[]`)

### Config
- `GET /api/config/phase-backends`
- `PATCH /api/config/phase-backends`
- `GET /api/config/routing`
- `PATCH /api/config/routing`
- `GET /api/config/worker`
- `PATCH /api/config/worker`

### System
- `GET /api/system/status`
- `POST /api/system/restart` — request a restart of running components (api + worker; dev watcher if present). Writes one `restart_requested_at` signal; each component self-restarts and Docker's `restart: unless-stopped` brings it back. Returns `{ requested_at, components, message }`. Mutating → requires `X-API-Key` when auth is enabled.

### Ingest (Remote Server Monitoring)
- `POST /api/ingest/scan`
- `GET /api/ingest/status`
- `GET /api/ingest/screengrabs`
- `POST /api/ingest/screengrabs/{file_id}/attach`
- `POST /api/ingest/screengrabs/attach-all`
- `POST /api/ingest/screengrabs/{file_id}/ignore`
- `POST /api/ingest/screengrabs/{file_id}/unignore`

### WebSocket
- `WS /api/ws/jobs`
- Events: `job_created`, `job_updated`, `job_completed`, `job_failed`, `stats_updated`
- Payload:
  - `{ "type": "job_updated", "job": { ... } }`
  - `{ "type": "stats_updated", "stats": { ... } }`

## MCP Interface (Cardigan)
Server: `cardigan` (stdio MCP)

### Common Workflow
1) `list_processed_projects` -> see ready projects
2) `load_project_for_editing` -> pull brainstorming, SST metadata, latest revision
3) `get_formatted_transcript` -> fact check
4) `save_revision` / `save_keyword_report` -> persist outputs

### Tools
- `list_processed_projects` (optional `status_filter`)
- `load_project_for_editing` (required `project_name`)
- `get_formatted_transcript` (required `project_name`)
- `save_revision` (required `project_name`, `content`)
- `save_keyword_report` (required `project_name`, `content`)
- `get_project_summary` (required `project_name`)
- `read_project_file` (required `project_name`, `filename`)
- `search_projects` (filters: `query`, `status`, `completed_after`, `completed_before`, `limit`)
- `get_sst_metadata` (required `media_id`, Airtable read-only)

### Prompts
- `hello_neighbor`
- `start_edit_session` (project name)
- `review_brainstorming` (project name)
- `analyze_seo` (project name)
- `fact_check` (project name)
- `save_my_work` (project name)

## File and Data Conventions
- Output root: `OUTPUT/{project_name}/`
- Manifest: `OUTPUT/{project_name}/manifest.json`
- Analyst/formatter/SEO outputs: `*_output.md`
- Revisions: `copy_revision_v{n}.md`
- Keyword reports: `keyword_report_v{n}.md`
- Transcripts: `transcripts/` (raw), formatted stored in `OUTPUT/{project}/formatter_output.md`

## Practical Integration Tips
- Prefer MCP tools for copy editing; use API for job lifecycle and monitoring.
- `GET /api/jobs/{id}/outputs/{filename}` is the fastest way to retrieve artifacts without touching the filesystem.
- Use WebSocket for UI updates, but polling `/api/queue/stats` is adequate for simple automation.
