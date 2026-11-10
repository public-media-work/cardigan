# MCP Server Reference (Cardigan)

## Overview
- Server name: `cardigan`
- Entry point: `mcp_server/server.py`
- Transport: stdio (MCP)
- Purpose: interactive copy-editing workflow over processed projects
- Reads/writes:
  - Reads from `OUTPUT/{project_name}/` and `transcripts/`
  - Writes revisions and keyword reports into `OUTPUT/{project_name}/`
- Airtable: READ-ONLY SST lookups (requires `AIRTABLE_API_KEY`)

## Environment
- `EDITORIAL_API_URL` (default `http://localhost:8100`) — for production work point this at `https://cardigan.riechers.co` (from the home network); `localhost` is local dev only. See `CLAUDE.md` › "Deployment Environments".
- `EDITORIAL_OUTPUT_DIR` (default `OUTPUT`)
- `EDITORIAL_TRANSCRIPTS_DIR` (default `transcripts`)
- `AIRTABLE_API_KEY` (optional; required for SST lookups)

## File Conventions
- Manifest: `OUTPUT/{project}/manifest.json`
- Default outputs:
  - `analyst_output.md`
  - `formatter_output.md`
  - `seo_output.md`
  - `manager_output.md`
  - `timestamp_output.md`
- Revisions:
  - `copy_revision_v{n}.md` (auto-incremented)
- Keyword reports:
  - `keyword_report_v{n}.md` (auto-incremented)

## Tools
All tools return a list of MCP `TextContent` items (typically a single text block).

### `list_processed_projects`
- Description: list processed projects and deliverables
- Input:
  - `status_filter` (optional): `all`, `ready_for_editing`, `revision_in_progress`, `processing`
- Output: formatted list with status, deliverables, completion time

### `load_project_for_editing`
- Description: full context bundle for editing (SST metadata, brainstorming, latest revision)
- Input:
  - `project_name` (required)
- Output: markdown with:
  - project status, job ID, completion time
  - inferred content type (segment vs digital short)
  - SST metadata (if linked and available)
  - brainstorming (analyst output)
  - latest revision (if any)
  - transcript access instructions

### `get_formatted_transcript`
- Description: returns AP Style formatted transcript, falls back to raw transcript
- Input:
  - `project_name` (required)
- Output: transcript text

### `save_revision`
- Description: save a copy revision with auto-versioning
- Input:
  - `project_name` (required)
  - `content` (required, markdown)
- Output: confirmation with filename, version, and path
- Side effects:
  - writes `copy_revision_v{n}.md`
  - updates `manifest.json` with revision metadata

### `save_keyword_report`
- Description: save a keyword report with auto-versioning
- Input:
  - `project_name` (required)
  - `content` (required, markdown)
- Output: confirmation with filename, version, and path
- Side effects:
  - writes `keyword_report_v{n}.md`
  - updates `manifest.json` with keyword report metadata

### `get_project_summary`
- Description: quick status and deliverables summary
- Input:
  - `project_name` (required)
- Output: status, job ID, completion time, deliverables, revision counts

### `read_project_file`
- Description: read a file from a project folder
- Input:
  - `project_name` (required)
  - `filename` (required)
- Output: file contents
- Security: rejects paths outside project directory

### `search_projects`
- Description: search projects by name, status, or completion date range
- Input (all optional):
  - `query` (text search)
  - `status` (`all`, `ready_for_editing`, `revision_in_progress`, `processing`, `failed`, `incomplete`)
  - `completed_after` (YYYY-MM-DD)
  - `completed_before` (YYYY-MM-DD)
  - `limit` (int, default 20)
- Output: formatted list of matching projects

### `get_sst_metadata`
- Description: fetch SST metadata by Media ID (Airtable)
- Input:
  - `media_id` (required)
- Output: formatted SST metadata with character counts
- Requires: `AIRTABLE_API_KEY`

## Prompts
Prompts are opinionated entry points for Cardigan workflows.

### `hello_neighbor`
- Friendly intro and list of ready projects

### `start_edit_session`
- Loads project context and guides through editing workflow
- Arguments: `project_name`

### `review_brainstorming`
- Reviews analyst output and compares against SST metadata
- Arguments: `project_name`

### `analyze_seo`
- Loads SEO output and suggests improvements
- Arguments: `project_name`

### `fact_check`
- Loads formatted transcript and verifies facts/quotes
- Arguments: `project_name`

### `save_my_work`
- Guides agent to compile and save a revision using `save_revision`
- Arguments: `project_name`

## Operational Notes
- `load_project_for_editing` infers content type using manifest fields, duration, or project name heuristics.
- When SST is not linked, the tool suggests an Airtable MCP lookup by Media ID.
- Tool output is intended for direct display to the user (markdown with headings and sections).
