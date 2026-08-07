# Cardigan
**PBS Wisconsin Digital Editorial Assistant v3.5**

![CI](https://github.com/MarkOnFire/ai-editorial-assistant-v3/actions/workflows/ci.yml/badge.svg)
![Version](https://img.shields.io/github/v/release/MarkOnFire/ai-editorial-assistant-v3?label=version)

A production-ready system for processing video transcripts and generating SEO-optimized metadata (titles, descriptions, keywords) for streaming platforms. Built for PBS Wisconsin's editorial team, powered by **Cardigan** -- our Mister Rogers-inspired AI copy editor.

---

## Overview

Cardigan transforms raw video transcripts into polished, SEO-ready metadata with minimal human intervention. Transcripts enter a four-phase LLM pipeline that extracts themes, formats descriptions, performs keyword analysis, and runs quality checks. The results are surfaced through a React dashboard for monitoring and a Claude Desktop MCP integration for human-in-the-loop editing.

The system is designed around a single principle: editorial teams should spend their time on editorial judgment, not formatting boilerplate.

---

## Features

### Processing Pipeline
- Four-phase LLM pipeline (Analyst, Formatter, SEO Specialist, Manager)
- Multi-model routing with tier-based cost optimization via OpenRouter
- Per-phase retry tracking with automatic tier escalation on failure
- Completeness checking to detect truncated or incomplete outputs
- Run cost caps and model allowlists for budget safety

### Web Dashboard
- Real-time job monitoring with WebSocket live updates
- Queue management with filtering, search, and bulk actions
- Job detail view with phase-by-phase progress and outputs
- Remote ingest watcher status and screengrab management
- Embedded chat prototype with Cardigan (REST-based)
- Model and phase statistics from Langfuse observability
- Drag-and-drop bulk transcript upload
- WCAG 2.1 Level AA accessibility (reduce motion, text sizing, high contrast)

### MCP Integration (Cardigan)
- 9 tools for project discovery, loading, editing, and saving
- 6 prompts for guided editorial workflows (brainstorming, SEO analysis, fact-checking)
- Direct integration with Claude Desktop for human-in-the-loop editing
- Auto-versioned revisions and keyword reports

### Remote Ingest Watcher
- Auto-discovery of transcripts and screengrabs from PBS media server
- Automatic screengrab attachment to matching projects by media ID
- Configurable scan paths and polling intervals
- Ignore/unignore workflow for file management

### Developer Tools
- GitHub Actions CI pipeline (lint, type-check, test on PR to main)
- Langfuse observability integration for LLM cost and performance tracking
- Airtable SST integration for metadata lookup (read-only)
- Structured event logging with per-job audit trails

---

## Architecture

```
+-----------------------------------------------------------------+
|                  THE METADATA NEIGHBORHOOD                      |
+-----------------------------------------------------------------+
|                                                                 |
|  +-----------+   +-----------+   +-----------+   +-----------+  |
|  |  Claude   |   |    Web    |   |  Ingest   |   | Transcript|  |
|  |  Desktop  |   | Dashboard |   |  Watcher  |   |  Upload   |  |
|  | (Cardigan)|   |   :3000   |   | (scanner) |   | (drag&drop|  |
|  +-----+-----+   +-----+-----+   +-----+-----+   +-----+-----+  |
|        |               |               |               |        |
|        |  MCP          |  REST/WS      |  REST         |  REST  |
|        v               v               v               v        |
|  +-----------------------------------------------------------+  |
|  |               FastAPI Server (:8000)                       |  |
|  |                                                            |  |
|  |  Routers:                                                  |  |
|  |  /api/queue    /api/jobs     /api/upload   /api/ingest     |  |
|  |  /api/chat     /api/system   /api/langfuse /api (config)   |  |
|  |  /api (ws)                                                 |  |
|  +--+-----------+-----------+-----------+--------------------+  |
|     |           |           |           |                       |
|     v           v           v           v                       |
|  +-------+  +-------+  +--------+  +----------+                |
|  |SQLite |  |Worker |  |OpenRo- |  | Langfuse |                |
|  |  DB   |  |(Queue)|  |uter API|  | (traces) |                |
|  +-------+  +-------+  +--------+  +----------+                |
|                                                                 |
+-----------------------------------------------------------------+

4-Phase Processing Pipeline:
  Transcript --> [Analyst] --> [Formatter] --> [SEO Specialist] --> [Manager] --> Output
                 (extract)    (format)        (keywords)           (QA review)
```

### Services Layer (14 modules)

| Service | Purpose |
|---------|---------|
| `llm.py` | Unified LLM client with cost tracking, model routing, safety guards |
| `worker.py` | Background job queue processor with retry and escalation |
| `database.py` | SQLite async database with event logging |
| `airtable.py` | Read-only Airtable SST integration |
| `langfuse_client.py` | Langfuse observability (httpx REST, not SDK) |
| `ingest_scanner.py` | Media server file discovery |
| `ingest_scheduler.py` | Periodic scan scheduling |
| `ingest_config.py` | Ingest watcher configuration |
| `screengrab_attacher.py` | Auto-attach screengrabs to projects by media ID |
| `chat_context.py` | Chat session context assembly |
| `chat_cost.py` | Chat token/cost tracking |
| `completeness.py` | Output completeness validation |
| `logging.py` | Structured logging setup |
| `utils.py` | Shared utilities |

---

## Quick Start

### Prerequisites

- Python 3.13+
- Node.js 18+
- OpenRouter API key

### Installation

```bash
# Clone the repository
git clone https://github.com/MarkOnFire/ai-editorial-assistant-v3.git
cd ai-editorial-assistant-v3

# Create virtual environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Install web dashboard dependencies
cd web && npm install && cd ..

# Configure environment
cp .env.example .env
# Edit .env with your OPENROUTER_API_KEY
```

### Running the System

```bash
# Start API server
uvicorn api.main:app --reload

# Start web dashboard (separate terminal)
cd web && npm run dev
```

### Access Points

| URL | Description |
|-----|-------------|
| http://localhost:8100 | API server |
| http://localhost:8100/docs | Interactive API docs (Swagger) |
| http://localhost:3100 | Web dashboard |

See [docs/QUICK_START.md](docs/QUICK_START.md) for detailed setup instructions.

---

## Docker Quickstart

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/install/) (v2+)

### Setup

```bash
# Configure environment
cp .env.example .env
# Edit .env — at minimum set OPENROUTER_API_KEY and CARDIGAN_API_KEY

# Build all containers
docker compose build

# Start the stack
docker compose up -d
```

### Verify

```bash
# API health check
curl http://localhost:8100/api/system/health

# Open the web dashboard
open http://localhost:3100
```

### Stop

```bash
docker compose down
```

Data is persisted in named Docker volumes (`db-data`, `output-data`, `transcript-data`). Use `docker compose down -v` to remove volumes and start fresh.

---

## Processing Pipeline

Each transcript passes through a four-phase agent pipeline:

| Phase | Role | Description |
|-------|------|-------------|
| 1. **Analyst** | Extract | Identifies key themes, notable quotes, and structural metadata from the transcript |
| 2. **Formatter** | Format | Produces polished title, description, and tags for streaming platforms |
| 3. **SEO Specialist** | Optimize | Generates keyword analysis, search optimization recommendations, and discoverability scoring |
| 4. **Manager** | QA | Final review for quality, completeness, and editorial standards compliance |

### Model Routing

The system routes each phase to cost-appropriate model tiers via OpenRouter presets:

| Tier | Preset | Use Case | Example Models |
|------|--------|----------|----------------|
| Cheapskate | `ai-editorial-assistant-cheapskate` | Simple formatting (Formatter, SEO) | Free-tier models |
| Default | `ai-editorial-assistant` | General processing (Analyst, Chat) | Gemini 3 Flash, Claude Sonnet 4.5 |
| Big Brain | `ai-editorial-assistant-big-brain` | Complex reasoning (Manager) | Gemini 3 Pro, GPT-5.1 Codex |

Phases escalate to higher tiers automatically on failure or timeout. See `config/llm-config.json` for full routing configuration.

---

## API Reference

### Queue (`/api/queue`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/queue/` | List jobs with pagination and filtering |
| POST | `/api/queue` | Create new job |
| GET | `/api/queue/stats` | Queue statistics |
| GET | `/api/queue/next` | Get next pending job |
| DELETE | `/api/queue/bulk` | Bulk delete jobs |
| DELETE | `/api/queue/{job_id}` | Delete a single job |

### Jobs (`/api/jobs`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/jobs/{id}` | Get job details |
| PATCH | `/api/jobs/{id}` | Update job |
| POST | `/api/jobs/{id}/pause` | Pause a running job |
| POST | `/api/jobs/{id}/resume` | Resume a paused job |
| POST | `/api/jobs/{id}/retry` | Retry a failed job |
| POST | `/api/jobs/{id}/cancel` | Cancel a job |
| GET | `/api/jobs/{id}/events` | Get job event log |
| GET | `/api/jobs/{id}/outputs/{filename}` | Download job output file |
| GET | `/api/jobs/{id}/sst-metadata` | Airtable SST metadata lookup |
| POST | `/api/jobs/{id}/phases/{phase}/retry` | Retry a specific phase |

### Upload (`/api/upload`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/upload/transcripts` | Bulk upload transcript files |

### Ingest (`/api/ingest`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/ingest/available` | List discovered files |
| POST | `/api/ingest/scan` | Trigger manual scan |
| GET | `/api/ingest/status` | Watcher status |
| GET | `/api/ingest/config` | Get ingest configuration |
| PUT | `/api/ingest/config` | Update ingest configuration |
| GET | `/api/ingest/screengrabs` | List discovered screengrabs |
| GET | `/api/ingest/screengrabs/for-media-id/{id}` | Screengrabs for a media ID |
| POST | `/api/ingest/screengrabs/{id}/attach` | Attach screengrab to project |
| POST | `/api/ingest/screengrabs/attach-all` | Batch attach all screengrabs |
| POST | `/api/ingest/screengrabs/{id}/ignore` | Ignore a screengrab |
| POST | `/api/ingest/screengrabs/{id}/unignore` | Unignore a screengrab |
| POST | `/api/ingest/transcripts/{id}/queue` | Queue a discovered transcript |
| POST | `/api/ingest/transcripts/queue` | Bulk queue transcripts |
| POST | `/api/ingest/transcripts/{id}/ignore` | Ignore a transcript |
| POST | `/api/ingest/transcripts/{id}/unignore` | Unignore a transcript |

### Chat (`/api/chat`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/chat/message` | Send message to Cardigan |

### System (`/api/system`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/system/health` | System health check |
| GET | `/api/system/status` | Detailed system status |
| POST | `/api/system/restart` | Request a restart of all running components |
| POST | `/api/system/watcher/heartbeat` | Watcher liveness ping (returns the restart flag) |

### Config (`/api`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/phase-backends` | Get phase-to-backend mapping |
| PATCH | `/api/phase-backends` | Update phase-to-backend mapping |
| GET | `/api/routing` | Get routing configuration |
| PATCH | `/api/routing` | Update routing configuration |
| GET | `/api/worker` | Get worker configuration |
| PATCH | `/api/worker` | Update worker configuration |

### Langfuse (`/api/langfuse`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/langfuse/status` | Langfuse connection status |
| GET | `/api/langfuse/model-stats` | Model usage statistics |
| GET | `/api/langfuse/phase-stats` | Per-phase performance stats |

### WebSocket (`/api`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| WS | `/api/ws/jobs` | Real-time job update stream |

See [docs/WEBSOCKET_IMPLEMENTATION.md](docs/WEBSOCKET_IMPLEMENTATION.md) for event types and protocol details.

---

## MCP Integration

Cardigan is the friendly copy editor who lives in The Metadata Neighborhood. Integrated with Claude Desktop via MCP, Cardigan helps polish AI-generated metadata with warmth and editorial care.

### Tools (9)

| Tool | Description |
|------|-------------|
| `list_processed_projects` | Discover projects ready for editing |
| `load_project_for_editing` | Load full project context (transcript, brainstorming, revisions) |
| `get_formatted_transcript` | Retrieve formatted transcript text |
| `save_revision` | Save an edited revision with auto-versioning |
| `save_keyword_report` | Save SEO keyword analysis |
| `get_project_summary` | Quick project overview |
| `read_project_file` | Read any project output file |
| `search_projects` | Search across all projects |
| `get_sst_metadata` | Fetch Airtable SST record for a project |

### Prompts (6)

| Prompt | Description |
|--------|-------------|
| `hello_neighbor` | Cardigan's warm welcome and orientation |
| `start_edit_session` | Begin an editing session with full project context |
| `review_brainstorming` | Review and refine AI-generated brainstorming |
| `analyze_seo` | Deep-dive SEO keyword analysis |
| `fact_check` | Verify claims and quotes against transcript |
| `save_my_work` | Save current edits with proper versioning |

See [docs/CLAUDE_DESKTOP_SETUP.md](docs/CLAUDE_DESKTOP_SETUP.md) for MCP server configuration.

---

## Web Dashboard

The React-based dashboard provides real-time monitoring and control across 8 pages:

| Page | Description |
|------|-------------|
| **Home** | Queue statistics, recent jobs, system health at a glance |
| **Queue** | Job list with filtering, search, bulk actions, and transcript upload |
| **Job Detail** | Phase-by-phase progress, outputs, events, embedded chat with Cardigan |
| **Projects** | Browse completed outputs organized by project |
| **Ready for Work** | Promoted discovery UX for projects needing editorial attention |
| **Settings** | Agent configuration, routing rules, accessibility preferences |
| **System** | API health, worker/watcher status, log viewer |
| **Help** | User guide and documentation |

Key components include the `IngestPanel` for remote file management, `ScreengrabSlideout` for contextual screengrab browsing, `ModelStatsWidget` and `PhaseStatsWidget` for Langfuse analytics, and the `chat/` module for the embedded Cardigan interface.

---

## Development

### Project Structure

```
ai-editorial-assistant-v3/
├── api/                        # FastAPI application
│   ├── main.py                 # App entry point with lifespan management
│   ├── routers/                # 9 API endpoint modules
│   ├── models/                 # Pydantic schemas
│   └── services/               # 14 business logic modules
├── web/                        # React dashboard (Vite + TypeScript)
│   └── src/
│       ├── components/         # UI components (chat, ingest, screengrabs)
│       ├── pages/              # 8 route pages
│       ├── hooks/              # Custom React hooks
│       └── context/            # React contexts
├── mcp_server/                 # Claude Desktop MCP server
│   └── server.py               # 9 tools, 6 prompts
├── claude-desktop-project/     # Claude Desktop project config
│   ├── EDITOR_AGENT_INSTRUCTIONS.md  # Canonical Cardigan prompt
│   ├── knowledge/              # Project knowledge files
│   └── templates/              # Output document templates
├── .claude/agents/             # LLM agent system prompts
├── config/                     # Configuration (llm-config.json)
├── scripts/                    # Utility scripts
├── docs/                       # Documentation (11 guides)
├── tests/                      # Test suite (pytest)
├── transcripts/                # Input files (gitignored)
└── OUTPUT/                     # Processed outputs (gitignored)
```

### Running Tests

```bash
# Activate virtual environment
source venv/bin/activate

# Run Python tests
pytest

# TypeScript build check
cd web && npm run build
```

### CI Pipeline

The GitHub Actions workflow (`ci.yml`) runs on pull requests to `main`:
- Python linting and formatting checks
- TypeScript type checking
- Test suite execution

### Development Session (AI agents)

```bash
# Initialize session (loads context)
./init.sh

# Check current progress
cat planning/claude-progress.txt
cat feature_list.json | jq '.[] | select(.status == "pending")'
```

See [CLAUDE.md](CLAUDE.md) for AI development guidelines.

### Python 3.14 Note

This project runs on Python 3.14. Some dependencies (notably the Langfuse SDK) are incompatible with 3.14 due to `pydantic.v1` issues. The Langfuse integration uses the REST API directly via `httpx` instead of the SDK.

---

## Configuration

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `OPENROUTER_API_KEY` | Yes | API key for LLM requests via OpenRouter |
| `AIRTABLE_API_KEY` | No | Read-only access for Airtable SST lookup |
| `LANGFUSE_PUBLIC_KEY` | No | Langfuse observability (public key) |
| `LANGFUSE_SECRET_KEY` | No | Langfuse observability (secret key) |
| `DATABASE_URL` | No | SQLite path (default: `./dashboard.db`) |

### LLM Configuration

Edit `config/llm-config.json` to customize:

- **OpenRouter presets** -- Model lists for each cost tier (cheapskate, default, big-brain)
- **Phase backends** -- Which tier handles each pipeline phase
- **Routing rules** -- Escalation behavior, timeout thresholds, completeness checks
- **Worker settings** -- Concurrency, poll intervals, heartbeat
- **Safety guards** -- Run cost caps, per-token cost limits, model allowlists

---

## Releases

| Version | Name | Highlights |
|---------|------|------------|
| [v3.5.0](https://github.com/MarkOnFire/ai-editorial-assistant-v3/releases/tag/v3.5.0) | Foundation Complete | CI pipeline, per-phase retry tracking, Langfuse integration |
| [v3.3.0](https://github.com/MarkOnFire/ai-editorial-assistant-v3/releases/tag/v3.3.0) | UX Refinement | "Ready for Work" discovery page, contextual screengrabs, UX polish |
| [v3.2.0](https://github.com/MarkOnFire/ai-editorial-assistant-v3/releases/tag/v3.2.0) | Chat Prototype | Embedded REST-based chat with Cardigan, DB persistence, slide-out panel |
| [v3.1.0](https://github.com/MarkOnFire/ai-editorial-assistant-v3/releases/tag/v3.1.0) | Remote Ingest Watcher | Auto-discovery from PBS media server, screengrab attachment |
| [v3.0.0](https://github.com/MarkOnFire/ai-editorial-assistant-v3/releases/tag/v3.0.0) | Core Architecture | FastAPI + SQLite + React rewrite, MCP integration, multi-model routing |

---

## Documentation

| Document | Description |
|----------|-------------|
| [QUICK_START.md](docs/QUICK_START.md) | 5-minute setup guide |
| [API_REFERENCE.md](docs/API_REFERENCE.md) | Full API documentation |
| [MCP_REFERENCE.md](docs/MCP_REFERENCE.md) | MCP tools and prompts reference |
| [CLAUDE_DESKTOP_SETUP.md](docs/CLAUDE_DESKTOP_SETUP.md) | Claude Desktop MCP integration setup |
| [WEB_UI_GUIDE.md](docs/WEB_UI_GUIDE.md) | Web dashboard user guide |
| [WEBSOCKET_IMPLEMENTATION.md](docs/WEBSOCKET_IMPLEMENTATION.md) | Real-time updates architecture |
| [AGENT_INTERFACE_GUIDE.md](docs/AGENT_INTERFACE_GUIDE.md) | LLM agent interface documentation |
| [AIRTABLE_CHEATSHEET.md](docs/AIRTABLE_CHEATSHEET.md) | Airtable table IDs and query patterns |
| [REMOTE_ACCESS.md](docs/REMOTE_ACCESS.md) | Remote access configuration |
| [FEATURE_REMOTE_INGEST_WATCHER.md](docs/FEATURE_REMOTE_INGEST_WATCHER.md) | Ingest watcher feature specification |

---

## License

Internal PBS Wisconsin tool -- not for distribution.

---

*Welcome to The Metadata Neighborhood. Cardigan is glad you're here.*
