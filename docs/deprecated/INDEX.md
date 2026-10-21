# Deprecated Artifacts — cardigan-v4

This directory consolidates **historical artifacts** from cardigan-v4's
predecessors and decommissioned subsystems. Nothing here is current truth;
everything here is **evidence** worth preserving for future refinement
work — most notably, comparing how the editor's role, the agent's failure
modes, and the deployment shape have evolved.

> Items in this directory are read-only by convention. Do not extend or
> modify them — they represent a moment in time. If the lesson learned
> from an artifact informs current cardigan-v4 behavior, that lesson
> belongs in the active codebase (or in an issue), not here.

## Contents

### Legacy editor agent (predecessor project)

The Claude Desktop project that preceded cardigan-v4. Lived as a sibling
repo at `~/Developer/pbswi/ai-editorial-assistant/` (still there, intact).
Copied here for cross-reference and historical diff against the current
agent.

| File | What it is |
|---|---|
| `EDITOR_AGENT_INSTRUCTIONS_v2-era.md` | The v2-era system prompt (32K, ~470 lines). Compare against the current `claude-desktop-project/EDITOR_AGENT_INSTRUCTIONS.md` (977 lines) to see how the role evolved — particularly the addition of AirTable propose/review/commit, anti-hallucination warnings, and tool-verification gates. |
| `ai-editorial-assistant-README.md` | Setup docs for loading the v2-era system prompt into Claude Desktop. Useful context for what the workflow used to be. |
| `ai-editorial-assistant-CLAUDE.md` | Predecessor project's dev notes. |

**Sibling repo cross-reference**: knowledge PDFs (AP Styleguide, transcript
style guide, WPM AI Guidelines, Media ID Prefixes, timestamp samples) lived
under `ai-editorial-assistant/knowledge/examples_and_styleguides/`. They
are also duplicated at `cardigan-v4/claude-desktop-project/knowledge/`
(verbatim). Not re-copied here.

### Langfuse hallucination findings (legacy data)

A separate 90-day archive of LLM-as-judge findings against ai-editorial-
assistant production traces. **Not in this directory** — kept in its own
home where ongoing analysis lives.

- **Active location**: `cardigan-v4/data/judge_archive/README.md`
- **Raw JSONL** (private): `~/Developer/second-brain/services/crows-nest/data/judge_archive/editorial_assistant_findings.jsonl`
- **Tracking issues** on `mriechers/cardigan`:
  - `#113` — fix: agents may fabricate metadata when filename / Media ID is missing
  - `#114` — investigate: review legacy editorial-assistant findings for patterns still applicable to cardigan-v4

### MCP server (decommissioned)

> **Pending — added in the MCP decommission PR.** Once the API + skills +
> cardigan-shepherd agent replace the MCP workflow, `mcp_server/server.py`
> stays in the repo but stops running. A pointer to it lands here with
> notes on what would need to change to re-enable it (e.g., for future
> Claude Desktop revival).

### Docker-deployed era job history (pending commit)

> **Pending — currently on Mark's laptop, not in the repo.** Job-history
> exports from the **Docker-deployed era** (the v4 architecture before it
> moved off Docker; same codebase generation, different deployment shape)
> preserve real production traces. See open issue
> [`mriechers/cardigan#164`](https://github.com/mriechers/cardigan/issues/164)
> for the commit reminder. Destination once committed:
> `cardigan-v4/docs/deprecated/docker-deployed-era/`.
>
> **Versioning note:** Docker was technically v4 of cardigan; the current
> codebase in `cardigan-v4/` is the same v4 architecture without the
> Docker wrapper. What replaces this migration's MCP server (skill-based
> architecture) is likely tagged v4.5 or v5 after QA — see
> the MCP decommission plan (removed in the roadmap consolidation; see git history) for the roadmap context.

## How to use this directory

1. **When investigating a failure mode in current cardigan-v4**: check
   whether the same pattern shows up in the Langfuse findings (linked
   above). If yes, the legacy evidence may already characterize the
   failure mode.
2. **When refining editor-agent behavior**: diff
   `EDITOR_AGENT_INSTRUCTIONS_v2-era.md` against the current canonical
   version to see what guardrails were added in response to specific
   problems. Removing those guardrails without understanding the history
   risks reintroducing the problems they were added to fix.
3. **When considering an architectural change**: the MCP server source
   (once added here) shows how the previous integration shape worked. If
   reverting to Claude Desktop becomes attractive, the deprecated server
   is the starting point.

## See also

- `cardigan-v4/data/judge_archive/README.md` — active analysis hub for
  legacy Langfuse findings
- Epic issues #222–#234 — the canonical roadmap (see the pinned Roadmap issue #357)
- Open issues on `public-media-work/cardigan` with label `legacy-data`
