# Claude Desktop Project

This folder contains the custom instructions and knowledge files needed to configure the **Editor Agent** as a Claude Desktop project (or other chat interfaces).

## Contents

| File | Purpose | When to Use |
|------|---------|-------------|
| `EDITOR_AGENT_INSTRUCTIONS.md` | Full system prompt for the copy-editor agent | Paste into Claude Desktop project instructions |
| `CLAUDE_DESKTOP_SETUP.md` | Step-by-step guide to connect MCP server and configure Claude Desktop | Initial setup only |

## Quick Setup

See `docs/CLAUDE_DESKTOP_SETUP.md` for the full step-by-step guide. In brief:

1. **Configure the Python MCP server** in Claude Desktop (see `docs/CLAUDE_DESKTOP_SETUP.md`)
2. **Create a Claude Desktop project** and paste the contents of `EDITOR_AGENT_INSTRUCTIONS.md` into the project instructions
3. **Add knowledge files** from the `knowledge/` folder to the project
4. **Test the connection**: Ask "What transcripts are ready for editing?"

## Updating Instructions

When modifying the editor agent's behavior:

1. Edit `EDITOR_AGENT_INSTRUCTIONS.md` in this folder
2. Copy the updated content to your Claude Desktop project
3. Test with a sample project to verify changes

## Related Documentation

- `HOW_TO_USE.md` - End-user workflow guide
- `QUICK_REFERENCE.md` - Command and tool quick reference
- DESIGN_v3.0.md Part 7 - Editor Agent UX design (removed in the roadmap consolidation; see git history)
