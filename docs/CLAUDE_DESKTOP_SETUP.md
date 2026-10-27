# Claude Desktop Setup for Cardigan

Meet **Cardigan**, your friendly editorial neighbor from **The Metadata Neighborhood**.

Cardigan is a warm, patient copy editor who speaks like Mister Rogers — genuinely delighted to help you polish your PBS Wisconsin metadata with care and kindness.

## Prerequisites

1. **Claude Desktop** installed on your Mac
2. **Python 3.10+** with the project's virtual environment
3. **API server running** — Start with `./scripts/start.sh` before using Cardigan
   - Cardigan queries the API for job status and metadata
   - Without the API, you can still browse OUTPUT folders but won't see job details

## Quick Setup

### 1. Locate your Claude Desktop config file

```bash
# The config file is at:
~/Library/Application Support/Claude/claude_desktop_config.json
```

### 2. Add Cardigan to your MCP servers

Edit the config file and add the `cardigan` server:

```json
{
  "mcpServers": {
    "cardigan": {
      "command": "/Users/YOUR_USERNAME/Developer/ai-editorial-assistant-v3/venv/bin/python",
      "args": [
        "-m",
        "mcp_server.server"
      ],
      "cwd": "/Users/YOUR_USERNAME/Developer/ai-editorial-assistant-v3"
    }
  }
}
```

**Important**: Replace `YOUR_USERNAME` with your actual macOS username.

### 3. Restart Claude Desktop

Quit and reopen Claude Desktop for the changes to take effect.

### 4. Say hello

In a new Claude Desktop conversation, just say:

> "Hello, Cardigan!"

Or use the `hello_neighbor` prompt to get a warm introduction.

## Available Tools

Once configured, Cardigan has access to these tools:

| Tool | Description |
|------|-------------|
| `list_processed_projects()` | Discover processed projects ready for editing |
| `load_project_for_editing(name)` | Load full context for an editing session |
| `get_formatted_transcript(name)` | Load AP Style transcript for fact-checking |
| `save_revision(name, content)` | Save copy revision with auto-versioning |
| `save_keyword_report(name, content)` | Save SEO/keyword report |
| `get_project_summary(name)` | Quick status check |
| `read_project_file(name, filename)` | Read specific project file |
| `search_projects()` | Search by name, status, or date range |

## Available Prompts

| Prompt | Description |
|--------|-------------|
| `hello_neighbor` | Warm introduction from Cardigan |
| `start_edit_session` | Begin editing a specific project |
| `review_brainstorming` | Review AI-generated titles/descriptions |
| `analyze_seo` | Deep-dive into SEO optimization |
| `fact_check` | Verify facts against the transcript |

## Usage Examples

### Start your day with Cardigan

Just say hello:

> "Hello, Cardigan! What's ready for me today?"

Cardigan will warmly greet you and show you what projects need attention.

### Start an editing session

> "I'd like to edit 2WLI1209HD"

Cardigan will:
1. Load the project context (brainstorming, existing revisions)
2. Present the AI-generated content for review
3. Ask how you'd like to proceed

### Review available projects

> "What projects are ready for editing?"

or

> "Show me projects that have revisions in progress"

### Fact-check against transcript

> "Can you check the speaker names in this description against the transcript?"

Cardigan will load the formatted transcript and verify accuracy with care.

### Save your work

When you approve a revision, Cardigan will save it:

> "This looks good, please save it"

Cardigan saves with auto-versioning (v1, v2, v3...) and confirms the file path.

## Troubleshooting

### Server not connecting

1. **Check the path**: Ensure the Python path in the config matches your actual venv location
2. **Check permissions**: The venv Python must be executable
3. **Check dependencies**: Run `./venv/bin/pip install mcp` if not already installed

### "Project not found" errors

1. **Ensure the API is running**: Start with `./scripts/start.sh` or `uvicorn api.main:app --reload`
2. **Check OUTPUT folder**: Projects must have a `manifest.json` in `OUTPUT/{project_name}/`
3. **Process a transcript first**: Upload via web dashboard or place in `transcripts/` folder

### Viewing server logs

Claude Desktop logs MCP server output. Check:
```bash
~/Library/Logs/Claude/
```

### Testing the server manually

```bash
cd /Users/YOUR_USERNAME/Developer/ai-editorial-assistant-v3
./venv/bin/python -m mcp_server.server
```

This runs in stdio mode — you'll see it waiting for input (that's normal).

## Project Knowledge Folder

For the best experience, add these to your Claude Desktop project's knowledge folder:

1. **`claude-desktop-project/EDITOR_AGENT_INSTRUCTIONS.md`** — Full editing workflow and Cardigan's personality (the copy-editor agent has been relocated to the pbswi workspace root and is no longer inside this repo)
2. **AP Stylebook reference** (if you have a PDF)
3. **Program-specific style guides** (University Place, Here and Now, etc.)

This gives Cardigan the context to follow PBS Wisconsin's editorial standards.

## Full Config Example

Here's a complete example config with Cardigan:

```json
{
  "mcpServers": {
    "cardigan": {
      "command": "/Users/mriechers/Developer/ai-editorial-assistant-v3/venv/bin/python",
      "args": ["-m", "mcp_server.server"],
      "cwd": "/Users/mriechers/Developer/ai-editorial-assistant-v3"
    }
  }
}
```

## Environment Variables (Optional)

The MCP server supports these environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `EDITORIAL_API_URL` | `http://localhost:8100` | The Metadata Neighborhood API URL. **For production work, set this to `https://cardigan.riechers.co`** (from the home network); `localhost` is local dev only. See `CLAUDE.md` › "Deployment Environments". |
| `EDITORIAL_OUTPUT_DIR` | `./OUTPUT` | Project output directory |
| `EDITORIAL_TRANSCRIPTS_DIR` | `./transcripts` | Transcript source directory |
| `AIRTABLE_API_KEY` | (none) | For SST metadata lookup (READ-ONLY) |

You can set these in the Claude Desktop config:

```json
{
  "mcpServers": {
    "cardigan": {
      "command": "/path/to/venv/bin/python",
      "args": ["-m", "mcp_server.server"],
      "cwd": "/path/to/ai-editorial-assistant-v3",
      "env": {
        "AIRTABLE_API_KEY": "your-api-key-here"
      }
    }
  }
}
```
