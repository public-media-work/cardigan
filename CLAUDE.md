# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

## Non-Claude Agents: Workspace Isolation Rules

**If you are NOT Claude Code** (e.g., Gemini CLI, Codex, Copilot, Cursor, or any other LLM agent), you MUST follow these isolation rules to avoid interfering with Claude agent work:

1. **Always work in a git worktree.** Never modify files on the current branch directly. Create a worktree before making any changes:
   ```bash
   git worktree add .worktrees/<your-agent-name>-<feature> -b <your-agent-name>/<feature-description>
   ```
2. **Always commit to a feature branch.** Your branch name must include your agent identifier (e.g., `gemini/fix-typo`, `codex/add-tests`, `copilot/refactor-utils`). Never commit to `main` or any branch you did not create.
3. **Never force-push, rebase, or modify branches you did not create.** Treat all existing branches as read-only references.
4. **Clean up after yourself.** When your work is complete, leave the worktree in place for human review. Do not merge your own branches.
5. **Follow all other rules in this file.** The conventions below (commit format, secrets management, attribution, etc.) apply to all agents equally.

> Claude Code agents are exempt from the worktree requirement because they are the primary agents for this workspace and coordinate directly with the user.


## Repository Purpose

Cardigan - A database-backed, API-first system for processing video transcripts and generating SEO-optimized metadata for PBS Wisconsin streaming platforms.

**Key differences from v2.0:**
- FastAPI-based API layer (not direct script execution)
- SQLite database as single source of truth
- React web dashboard for monitoring
- Claude Desktop for copy-editor workflow (MCP integration)

## Deployment Environments (READ BEFORE CALLING THE API)

There are two running instances. **For any production/editorial work — reviewing
real jobs, reading SST metadata, copy-editing live content — agents MUST target
the homelab-hosted container, not localhost.**

| Environment | Base URL | Use for |
|-------------|----------|---------|
| **Production (default)** | `http://cardigan01:8100` | All real editorial/production work. Reach over Tailscale (MagicDNS name `cardigan01`, CTID 103) or LAN. |
| Local dev | `http://localhost:8100` | Only when actively developing/testing the API locally. Never for production data. |

Rules for agents:
- **Default to `http://cardigan01:8100`.** Only use `localhost` when the user is
  explicitly doing local development.
- **Target the name `cardigan01`, never a hard-coded tailnet IP** — tailnet IPs
  drift (it has already changed once). LAN fallback if MagicDNS is down:
  `192.168.1.42:8100`.
- Honor a caller-supplied `CARDIGAN_API_URL` if set; otherwise the production
  default applies.
- **Auth:** when `CARDIGAN_API_KEY` is set on the deployment, send it as an
  `X-API-Key` header (exempt paths: `/`, `/api/system/health`, `/docs`,
  `/openapi.json`, `/api/ws/*`). The homelab box is currently unauthenticated +
  tailnet/LAN-only; read the key from the environment, never hard-code it.

See `docs/AGENT_INTERFACE_GUIDE.md` for the endpoint catalog.

## Key Commands

### Development

```bash
# Initialize development session
./init.sh

# Start API server (once implemented)
uvicorn api.main:app --reload

# Run tests
pytest

# Start web dev server (once implemented)
cd web && npm run dev
```

### Virtual Environment

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Architecture

```
editorial-assistant-v3/
├── api/                        # FastAPI application
│   ├── main.py                 # App entry point
│   ├── routers/                # API endpoints
│   ├── models/                 # Pydantic schemas
│   └── services/               # Business logic
├── web/                        # React dashboard
│   └── src/
├── claude-desktop-project/     # Claude Desktop project config
│   ├── EDITOR_AGENT_INSTRUCTIONS.md  # Canonical editor prompt
│   ├── knowledge/              # Project knowledge files
│   └── templates/              # Output document templates
├── .claude/
│   ├── agents/                 # LLM agent system prompts
│   ├── templates/              # Output document templates
│   └── commands/               # Slash command definitions
├── config/                     # Configuration files
├── transcripts/                # Input files (gitignored)
├── OUTPUT/                     # Processed outputs (gitignored)
├── tests/                      # Test suite
├── docs/                       # Documentation
│   ├── agents/                 # Issue tracker, triage labels, domain-doc rules
│   └── adr/                    # Architecture decision records (created lazily)
└── standalone-agents/          # Out-of-pipeline agent POCs
```

## Where the work is tracked

**The roadmap is GitHub issues, not a file in this repo.** There is no
`feature_list.json`, no progress file, and no `planning/` directory — they were
competing, perpetually-stale copies of the issue tracker and were removed. If you
find yourself about to write a status document, put it in the issue instead.

- **The roadmap** — the pinned [Roadmap] issue #357. Start here for orientation.
- **Epics** — `#222`–`#234`, one per work area, each with native sub-issues.
  Epic C (`#224`) is closed; the rest are open.
- **Conventions** — `docs/agents/issue-tracker.md` (the `gh` recipes),
  `docs/agents/triage-labels.md` (the five canonical labels),
  `docs/agents/domain.md` (what to read before exploring).

The repo is `public-media-work/cardigan`. Note that the GHCR image prefix is
`ghcr.io/mriechers/cardigan` — a different, still-current path. Don't "correct"
one into the other.

### Picking up work

```bash
./init.sh                                              # venv + git status + epics
gh issue list --state open --label ready-for-agent     # fully-specified work
gh issue view <n> --comments                           # read before starting
```

**Verify-already-fixed first.** A large share of this backlog was fixed in a later
sprint but never closed. Before working an issue, grep the cited file or symbol on
`main` — it may already be done. Close it with a comment if so.

### Before you push

CI gates on all three of these. Run them locally or CI will fail you:

```bash
ruff check .
black --check .     # CI runs --check; use `black .` to fix
pytest --tb=short -q
```

## Git Commit Convention

**See**: `/Users/mriechers/Developer/the-lodge/conventions/COMMIT_CONVENTIONS.md`

AI commits should include agent attribution:
```
feat: Add new feature

[Agent: Main Assistant]

Detailed description...
```

## Design Reference

Design docs live next to the code they describe, or in the epic that owns the work:

- `docs/SYSTEM_RESTART_DESIGN.md` — system-components restart design (shipped)
- `standalone-agents/youtube-copy-audit/FEATURE.md` — YouTube metadata POC scope
- `docs/adr/` — architecture decision records (created lazily; none written yet)
- Epic scope lives in the epic issue (e.g. Epic L `#233`, Epic M `#234`,
  Epic K `#232` for v5 hosting)

Superseded design docs (`DESIGN_3.5.md`, `DESIGN_4.0.md`, `DESIGN_v3.0.md`) were
removed in the roadmap consolidation and remain in git history.

## Airtable Integration (CRITICAL)

**CONTROLLED WRITE ACCESS via `commit_sst_edits` only.**

- Agents may READ Airtable data freely (SST records, metadata)
- Agents may WRITE to Airtable **only** through the `commit_sst_edits` MCP tool, which enforces:
  - **Field allowlist**: Only Release Title, Short Description, Long Description, Keywords, and social media fields are writable
  - **Optimistic concurrency**: Re-fetches current values before writing; refuses if fields changed since proposal
  - **Audit trail**: Posts a comment on the Airtable record with old/new values and reasons
  - **User confirmation**: Agent must show `review_proposed_edits` output and get user approval before committing
- Direct Airtable API writes outside the `propose → review → commit` workflow are prohibited
- Agents must NEVER use `create_record`, `delete_records`, or write to non-allowlisted fields

The workflow: `propose_sst_edit` (stage) → `review_proposed_edits` (preview) → user confirms → `commit_sst_edits` (write)

### Quick Reference

**See `docs/AIRTABLE_CHEATSHEET.md`** for token-efficient AirTable lookups:
- Direct table IDs (skip `list_tables` calls)
- Key fields for editorial workflows
- Ready-to-use filter formulas
- Program-specific query patterns

**Key Table IDs:**
| Table | ID |
|-------|-----|
| Single Source of Truth | `tblTKFOwTvK7xw1H5` |
| Projects | `tblU9LfZeVNicdB5e` |
| Segments | `tblb6x1BhkdhKrmT6` |
| Contacts | `tblJc6JpKVcmwg0XV` |
| Staff | `tblEjbbFzmpGZgbXF` |

**Base ID:** `appZ2HGwhiifQToB6`

## Cost Data Versioning

Every row in `jobs`, `session_stats`, and `chat_sessions` is tagged with
an `app_version` (derived from the git tag, e.g. `"v4.2"`; overridable via
the `CARDIGAN_VERSION` env var). See `docs/COST_DATA_VERSIONING.md` for how
to bump the version, restore snapshots, and run backfills.

## Design Context

### Users
PBS Wisconsin content editors who use Cardigan as one tool among many in their daily workflow. They're multitaskers working across AirTable, CMS tools, and various internal systems — most of which are ugly and utilitarian. Cardigan processes their well-edited transcripts through a 4-phase LLM pipeline to generate SEO metadata, eliminating tedious duplicative work. They need to monitor jobs, review output, retry phases, and manage the queue. The tool should feel like a calm, reliable workstation — not another source of friction.

### Brand Personality
**Helpful, clever, pragmatic.** Cardigan is named after the cardigan sweater — warm, familiar, dependable. There's a Mr. Rogers' Neighborhood thread running through the naming ("The Metadata Neighborhood") that should be honored where it won't get in the way. The tool embodies doing more with less: taking one input and transforming it into many outputs to reduce tedious work. It should feel like a kind, competent colleague.

### Aesthetic Direction
- **Theme**: Dark mode (control room monitoring context, used alongside other tools)
- **Tone**: Clean, calm confidence. Not flashy, not generic. Should feel like it belongs in the PBS family of tools — trustworthy and approachable without being corporate or sterile.
- **Brand colors**: PBS Wisconsin blue `#1d4f91`, PBS red `#c8102e` (accent only)
- **Anti-references**: Google Drive, generic CMS tools, videosearch.pbswi.wisc.edu (quick-and-dirty internal tool), any tool that feels like it was built by committee or generated by AI
- **References**: AirTable (clean, clear, functional), PBS Wisconsin site (trustworthy, community-focused)
- **WCAG compliance is mandatory** — the team focuses on accessibility

### Design Principles
1. **Calm confidence over flashy features** — The interface should feel like it has everything under control. Status is clear, actions are obvious, nothing demands attention unnecessarily.
2. **Warm professionalism** — Not sterile enterprise UI, not playful startup UI. Public television personality where it helps, invisible where it would get in the way.
3. **Respect the editor's time** — These users are asked to do more with less. Every interaction should save time, not add steps. Dense when needed, spacious when helpful.
4. **Belong to the PBS family** — Visual DNA should connect to PBS Wisconsin's brand identity. The blue, the trustworthiness, the community-focused sensibility.
5. **Accessible by default** — WCAG AA minimum across all surfaces. Reduced motion, high contrast, and text scaling are first-class features, not afterthoughts.

## Notes for Claude Code

1. **Find work in GitHub issues** — see "Where the work is tracked" above
2. **Verify-already-fixed first** — grep `main` before working any issue
3. **Run `ruff check .`, `black --check .`, and `pytest`** before marking complete
4. **Don't break the API contract** - OpenAPI spec is the source of truth (once defined)
5. **Log feedback** - Append issues to `AGENT-FEEDBACK.md` if created
6. **NEVER write to Airtable** - Read-only access for all AI agents

## Agent skills

### Issue tracker

Issues live in GitHub Issues on `public-media-work/cardigan` (via the `gh` CLI).
See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical triage roles, used verbatim: `needs-triage`, `needs-info`,
`ready-for-agent`, `ready-for-human`, `wontfix`. The repo's older
`executor: *` labels — `agent`, `human`, and `either` — are legacy and are
being migrated off. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context — `CONTEXT.md` + `docs/adr/` at the repo root. **Neither exists
yet** — both are created lazily, when there is something worth recording. Don't go
looking for them; write the first one when you have the first decision.
See `docs/agents/domain.md`.
