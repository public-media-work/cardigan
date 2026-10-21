# Audio Upload Mode

Editors can hand Cardigan an audio or video recording instead of a finished
transcript. The recording is transcribed locally with WhisperX (speaker
diarization included), the editor reviews and corrects the transcript
in-app, and the approved transcript then flows through the normal LLM
pipeline (analyst → formatter → seo → validator). Corrections feed a
running glossary that improves every future transcription.

## State machine

```
upload (POST /api/upload/media)
  │  video? → ffmpeg strips the audio track (original deleted)
  ▼
pending ──claim──► in_progress (current_phase=transcription)
  ▲                    │
  │ defer (busy /      │ WhisperX ok
  │ service down)      ▼
  └────────────── awaiting_review ◄─── editor edits segments + names speakers
                       │        │
        approve        │        │ retranscribe (extra prompt terms)
  (SRT + glossary      │        └────────► pending (transcription reset)
   mining + phase      ▼
   reset)          pending ──► normal LLM pipeline ──► completed
```

- Media jobs have `job_type='media'`, a `transcription` phase prepended to
  the standard four, and pause in the dedicated `awaiting_review` status.
- Transcription failures use the normal failed/retry path; a busy or
  stopped diarization service uses the defer-and-requeue path (so stopping
  the diarization container to free homelab memory just politely delays
  media jobs).

## Storage layout

| Artifact | Where | Purpose |
|----------|-------|---------|
| Uploaded audio (`.m4a`/`.mp3`/…) | `media/` (volume `/data/media`) | Transcription input + review playback |
| `transcription_raw.json` | `OUTPUT/<project>/` | Verbatim WhisperX result (provenance, diff baseline) |
| `transcription_edited.json` | `OUTPUT/<project>/` | Editor's working copy: segments + `speaker_map` |
| `transcript_approved.srt` | `OUTPUT/<project>/` | Provenance copy of the approved transcript |
| `<project>.srt` | `transcripts/` | Approved, speaker-labeled transcript the pipeline consumes |
| `job_type`, `media_file`, `intake` | `jobs` table (migration 022) | Job classification, audio filename, upload-form metadata |

## Transcription service

The existing diarization container (`diarization/`, compose profile
`diarization`) gained `POST /transcribe`: multipart file +
`initial_prompt`, `language`, `diarize`, `min_speakers`, `max_speakers`.
It returns full transcript segments (text, word timestamps, speaker
labels), detected language, and `diarized: false` when pyannote is
unavailable (no HF token) — callers degrade to a single speaker bucket.

Operational notes:

- **Single-flight**: one WhisperX run at a time; concurrent requests get
  `503 + Retry-After: 300`, which the worker maps to `defer_job`.
- The pipeline runs in a worker thread so `/health` stays responsive
  during hour-long CPU runs (previously the event loop blocked and
  Docker's healthcheck could kill the container mid-job).
- `WHISPER_MODEL_SIZE` (default `base`, CPU int8): `small` roughly doubles
  accuracy on proper nouns at ~2–3× the runtime. Hour-long audio on CPU
  can approach real-time — the API client waits up to
  `DIARIZATION_TRANSCRIBE_TIMEOUT` (default 3600s).
- whisperx is pinned (see `diarization/requirements.txt`); the per-request
  `initial_prompt` swap and the `whisperx.diarize.DiarizationPipeline`
  import must be re-verified on upgrade.
- Do **not** run WhisperX concurrently with the 19 GB local LLM on the
  36 GB homelab box (see the local-LLM tier handoff, removed in the roadmap consolidation; see git history).
  Stop the diarization profile; media jobs defer until it returns.

## Glossary feedback loop

`knowledge/glossary.md` (bind-mounted into api + worker since this
feature) carries a **Whisper Prompt Terms** bullet section — Cardigan's
project layer of the workspace base glossary
(`pbswi/automations/transcripts/glossary.md`; project wins on conflict).

The `initial_prompt` is built per job with a ~500-character budget,
priority: **speaker names > per-job context terms > glossary terms**
(`api/services/whisper_prompt.py`).

Terms enter the glossary three ways:

1. Upload form opt-in (`add_to_glossary`) — speakers + context terms.
2. **Approve-time diff mining** (`api/services/transcript_diff.py`):
   word-level diff of raw vs. edited segments; ≤3-word replacements that
   look like proper nouns and recur (or match an intake term) become
   Editor Corrections rows *and* whisper prompt terms.
3. The existing phase-retry feedback extraction (LLM-parsed), which now
   writes through the same shared service.

## Endpoints

| Method/Path | Purpose |
|-------------|---------|
| `POST /api/upload/media` | Media upload + intake form; streams to disk, extracts audio from video |
| `GET /api/glossary` / `POST /api/glossary/terms` | Whisper-terms preview / opt-in appends |
| `GET /api/jobs/{id}/transcription` | Raw + edited segments, speaker map, intake |
| `PUT /api/jobs/{id}/transcription` | Autosave the edited working copy |
| `POST /api/jobs/{id}/transcription/approve` | SRT + glossary mining + phase reset + requeue |
| `POST /api/jobs/{id}/transcription/retranscribe` | Re-run transcription with extra prompt terms |
| `GET /api/jobs/{id}/media` | Audio playback (Range-backed scrubbing) |

Upload size: `MEDIA_MAX_UPLOAD_BYTES` (default 2 GB), nginx `/api/`
raised to 3 GB. **LAN/Tailscale only** — the Cloudflare tunnel caps
request bodies around 100 MB.

---

# Spec-only: not implemented on this branch

## 1. API-driven transcription backends

Today the transcription stage is hardwired to the local WhisperX service.
When a cloud variant is wanted (box busy, no GPU, travel), introduce a
backend protocol in `api/services/transcription_backends.py`:

```python
class TranscriptionBackend(Protocol):
    name: str
    async def is_available(self) -> bool: ...
    async def transcribe(
        self, audio_path: Path, *, initial_prompt: str, language: str,
        diarize: bool, min_speakers: int | None, max_speakers: int | None,
    ) -> TranscribeOutcome: ...
```

`_run_transcription_stage` selects the backend; everything downstream
(raw/edited JSON, review UI, approve) is backend-agnostic because the
outcome shape is already normalized.

Candidate implementations, in rough order of fit:

| Backend | Timestamps | Diarization | Notes |
|---------|-----------|-------------|-------|
| `LocalWhisperXBackend` | word-level | pyannote | Current behavior, extracted |
| `DeepgramBackend` (nova-3) | word-level | built-in | Closest cloud match; per-minute pricing |
| `GroqWhisperBackend` / OpenAI `whisper-1` | segment/word (`verbose_json`) | none — synthesize one bucket or run pyannote locally | Cheap, fast; supports `prompt` |
| `OpenRouterAudioBackend` | **none** (chat `input_audio` parts on Gemini-class models) | weak/none | Full text only; segments must be synthesized (e.g., sentence-split + proportional timing), so review loses seek-accurate timestamps. Fine for text-first review, unsuitable for caption-grade SRT. Prompt terms go in the chat system prompt instead of `initial_prompt`. |

Config mirrors the LLM backend map in `config/llm-config.json`:

```json
"transcription": {
  "backend": "local-whisperx",
  "fallback_backend": null,
  "backends": {
    "local-whisperx": {"type": "diarization-service"},
    "deepgram": {"type": "deepgram", "model": "nova-3", "api_key_secret": "deepgram_api_key"}
  }
}
```

Secrets follow `api/services/secrets.py` (Docker secret → env → Keychain).
Cloud costs should be recorded on the transcription phase record (`cost`
is already persisted per phase; local runs stamp 0).

Privacy note: raw station audio leaves the building with any cloud
backend — clear it editorially before enabling one.

## 2. Reusing the editor for other pipeline outputs

The review editor's core (list of editable blocks + autosave + approve
hook) generalizes to revising pipeline outputs in-app — starting with the
**formatted transcript** (`formatter_output.md`), replacing part of the
Claude Desktop copy-editor handoff:

- Generalize `SegmentList`/`SegmentRow` into a `DocumentEditor` whose
  block model is pluggable: timestamped segments (current) vs. markdown
  blocks (split on blank lines / headings). Timestamp column and speaker
  select become optional block adornments.
- Persistence parallels the transcription endpoints:
  `GET/PUT /api/jobs/{id}/outputs/{key}/edit` storing
  `<key>_edited.json` next to the output, and
  `POST .../outputs/{key}/apply` writing the edited markdown back to
  `<phase>_output.md` (archiving the original like `previous_runs`).
- Apply-time diff mining reuses `transcript_diff.mine_corrections`
  unchanged — proper-noun fixes in the formatted transcript are exactly
  the corrections the glossary wants.
- Downstream phases that already consumed the old output need the same
  reset treatment as approve: `reset_phases_for_reprocess(phases,
  skip=(everything up to and including the edited phase))`.

Decide before building: whether edited outputs re-run the validator
automatically, and whether copy-editor MCP revisions
(`copy_revision_v*.md`) should surface in the same editor rather than a
parallel path.
