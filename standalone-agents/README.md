# Standalone Agents

Portable versions of Cardigan pipeline agents, designed to work in any AI chat system (Gemini Gems, ChatGPT GPTs, Claude Projects, etc.) without requiring the full Cardigan infrastructure.

## Available Agents

### Transcript Formatter

Transforms raw SRT transcripts into clean, publication-ready markdown following PBS Wisconsin editorial standards.

**Files:**
- `transcript-formatter/PROMPT.md` — System prompt for Gem/GPT/Project setup
- `transcript-formatter/format-transcript.py` — CLI script for batch processing (recommended for long transcripts)
- `transcript-formatter/reference/` — Supporting reference docs to attach as knowledge

### YouTube Copy Audit (proof of concept)

Validates the planned YouTube ingest + copy-audit + write-back feature before
any Cardigan backend work: fetch a channel video's metadata and captions, run
the captions through the production Cardigan pipeline, produce a three-way
copy audit report (YouTube vs. Airtable SST vs. generated), and — after human
approval of the diff — write metadata back to the live video via the pbswi
`youtube-post` guarded executor.

See `youtube-copy-audit/README.md` for setup and the run book, `PROMPT.md` for
agent guardrails, and `standalone-agents/youtube-copy-audit/FEATURE.md` for the full
feature design this POC gates.

---

### Option A: CLI Script (recommended)

Splits the SRT into chunks, sends them to Gemini in parallel, and stitches the results into a single markdown file. No manual "continue" prompting needed.

**Setup:**
```bash
pip install google-genai
export GEMINI_API_KEY="your-key-here"
```

**Usage:**
```bash
# Basic — outputs input.md alongside input.srt
python format-transcript.py input.srt

# With speaker names and program
python format-transcript.py input.srt \
  --speakers "Host: Frederica Freyberg, Guest: Tony Evers" \
  --program "Here and Now"

# Custom output path and model
python format-transcript.py input.srt -o formatted.md --model gemini-2.5-pro
```

**Options:**
| Flag | Default | Description |
|------|---------|-------------|
| `--speakers`, `-s` | — | Speaker names (e.g., `"Host: Name, Guest: Name"`) |
| `--program`, `-p` | — | Program name |
| `--output`, `-o` | `{input}.md` | Output file path |
| `--model`, `-m` | `gemini-2.5-flash` | Gemini model to use |
| `--parallel` | `3` | Max concurrent API requests |

---

### Option B: Gemini Gem (for short transcripts or interactive use)

**Setup:**
1. Create a new Gem in Google AI Studio
2. Paste the contents of `PROMPT.md` into the system instructions
3. Upload the files from `reference/` as knowledge documents
4. Name the Gem "PBS Transcript Formatter"

**Usage:**
Paste or upload an SRT file into the chat. Optionally provide speaker names and/or the media ID.

**Note:** For transcripts longer than ~5 minutes, the Gem will need to output in multiple turns. The prompt instructs it to pause and ask you to say "continue." For a smoother experience with long transcripts, use the CLI script (Option A).
