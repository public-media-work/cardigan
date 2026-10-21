# Quick Start — The Metadata Neighborhood

Get **The Metadata Neighborhood** running in 5 minutes.

## First-Time Setup

### 1. Install dependencies

```bash
cd ai-editorial-assistant-v3
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.lock
```

### 2. Set up the local domain (optional but nice)

```bash
./scripts/setup-local-domain.sh
```

This adds `metadata.neighborhood` to your hosts file so you can access the API at a memorable URL.

### 3. Configure environment

Create a `.env` file with your API keys:

```bash
# Required for LLM processing
OPENROUTER_API_KEY=sk-or-v1-your-key-here

# Optional: For SST metadata lookup (READ-ONLY)
AIRTABLE_API_KEY=pat-your-key-here
```

### 4. Start the Neighborhood

```bash
./scripts/start.sh
```

This runs database migrations, starts the API server, and launches the worker.

## Daily Usage

| Command | What it does |
|---------|--------------|
| `./scripts/start.sh` | Start API + worker |
| `./scripts/stop.sh` | Stop everything |
| `./scripts/status.sh` | Check if running |

### URLs

| URL | Description |
|-----|-------------|
| http://metadata.neighborhood:8100 | API (after domain setup) |
| http://localhost:8100 | API (always works) |
| http://localhost:3100 | Web dashboard (run `cd web && npm run dev`) |

### Shell Aliases (add to ~/.zshrc)

```bash
# The Metadata Neighborhood
export NEIGHBORHOOD="$HOME/Developer/ai-editorial-assistant-v3"

alias neighborhood-start="$NEIGHBORHOOD/scripts/start.sh"
alias neighborhood-stop="$NEIGHBORHOOD/scripts/stop.sh"
alias neighborhood-status="$NEIGHBORHOOD/scripts/status.sh"
alias neighborhood-logs="tail -f $NEIGHBORHOOD/logs/api.log $NEIGHBORHOOD/logs/worker.log"
alias neighborhood-queue="curl -s http://metadata.neighborhood:8100/api/queue/stats | python3 -m json.tool"
```

## Meet Cardigan

**Cardigan** is the friendly copy editor agent who lives in The Metadata Neighborhood.

To use Cardigan in Claude Desktop:

1. Follow the setup in [CLAUDE_DESKTOP_SETUP.md](./CLAUDE_DESKTOP_SETUP.md)
2. Say "Hello, Cardigan!" to get started

Cardigan speaks like Mister Rogers — warm, patient, and genuinely delighted to help you polish your metadata.

## Processing Transcripts

### Add a transcript to the queue

```bash
curl -X POST http://metadata.neighborhood:8100/api/queue \
  -H "Content-Type: application/json" \
  -d '{"transcript_file": "MY_TRANSCRIPT.txt"}'
```

### Watch folder (batch processing)

```bash
# Queue all unprocessed transcripts
./venv/bin/python watch_transcripts.py --once

# Watch continuously for new files
./venv/bin/python watch_transcripts.py
```

### Check queue status

```bash
curl http://metadata.neighborhood:8100/api/queue/stats
```

## Troubleshooting

### Server won't start

1. Check if port 8000 is in use: `lsof -i :8000`
2. Run migrations: `./venv/bin/alembic upgrade head`
3. Check logs: `tail -f logs/api.log`

### "metadata.neighborhood" doesn't work

Run the domain setup script:
```bash
./scripts/setup-local-domain.sh
```

### Cardigan isn't responding in Claude Desktop

1. Restart Claude Desktop after config changes
2. Check the MCP server path in your config
3. Test manually: `./venv/bin/python -m mcp_server.server`

---

*Welcome to The Metadata Neighborhood. Cardigan is glad you're here.* 🏘️
