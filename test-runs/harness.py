"""Supervised test-run harness for Cardigan (used by the /test-run skill).

Drives one caption file through prod's own prompt builders and gates, with the model
calls made by local subagents (or, as a fallback per phase, by ``replay`` to OpenRouter,
sending the captured prompt unchanged). Every prompt is CAPTURED, not re-implemented: the
harness runs the worker's real ``_run_phase`` (and through it
``_run_formatter_chunked``) with ``llm.chat`` replaced by a recorder, so the files it
writes are exactly what the code under test would send. Prompt capture and gates make
no LLM, network or DB calls; only ``code`` (git fetch), ``setup`` (SRT download) and
``replay`` (OpenRouter, paid, key via get-secret.sh) touch the network.

Each run is a directory holding ``run.json`` (written by ``setup``), the source
``.srt``, and one subdirectory per phase. Every command takes ``--run-dir``.

Commands:
  code      --dest DIR [--prs 409,421]   export origin/main (or a merged PR stack) to DIR
  setup     --run-dir D --srt URL|PATH --code DIR [--media-id ID] [--prs ...]
  sst-fields                             print the Airtable field names to fetch
  sst       --run-dir D [--record R.json] [--project P.json]
  intake    --run-dir D
  prompts   --run-dir D PHASE [--variant V] [--flags F.json] [--feedback TEXT]
                                         [--model M] [--force-max-tokens N]
  brief     --run-dir D PHASE [--variant V] [--chunk N]  print the subagent dispatch prompt
  merge     --run-dir D [--variant V]    real merge_formatter_chunks over chunkN/output.md
  gate      --run-dir D PHASE [--variant V]   PHASE: analyst|formatter|seo|validator|timestamp
  replay    --run-dir D PHASE [--variant V] [--chunk N] [--model M | --newest] [--max-tokens N]
                                         fallback: send the captured prompt, unchanged, to OpenRouter
  promote   --run-dir D PHASE [--variant V] [--chunk N]   make replay/output.md the phase output
  index     --run-dir D                  print an index row (JSON) for the private cohort

Phases run in prod's order: analyst, formatter, seo, validator, then timestamp (which
runs after the validator and is never seen by it), then the QA gate (``gate validator``).
"""

import argparse
import asyncio
import inspect
import json
import re
import shutil
import subprocess
import sys
import urllib.request
from collections import Counter
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
PHASES = ["analyst", "formatter", "seo", "validator", "timestamp"]

# Hand-copied from prod's routing: config/llm-config.json `phase_models` (haiku-4.5 analyst and
# validator, sonnet-4.6 formatter and timestamp, sonnet SEO since #422) and `qa_escalation.family_order`.
# Update these when that routing changes; `intake` prints the live phase_models to compare.
SUBAGENT_TIER = {
    "analyst": "haiku",
    "formatter": "sonnet",
    "seo": "sonnet",
    "validator": "haiku",
    "timestamp": "sonnet",
}
ESCALATION = {"haiku": "sonnet", "sonnet": "opus", "opus": None}


# --------------------------------------------------------------------------- run state


class Run:
    def __init__(self, run_dir):
        self.dir = Path(run_dir).resolve()
        meta = self.dir / "run.json"
        if not meta.exists():
            sys.exit(f"no run.json in {self.dir}; run `setup` first")
        self.meta = json.loads(meta.read_text())
        self.code = Path(self.meta["code"])
        self.srt_path = self.dir / self.meta["srt"]
        self.srt = self.srt_path.read_text(encoding="utf-8", errors="replace")
        self.job = {
            "id": 9999,
            "project_name": self.meta["project_name"],
            "transcript_file": self.meta["srt"],
            "media_id": self.meta.get("media_id"),
        }

    def phase_dir(self, phase, variant=None):
        return self.dir / phase / variant if variant else self.dir / phase

    def output(self, phase, variant=None):
        if variant and (self.phase_dir(phase, variant) / "output.md").exists():
            return (self.phase_dir(phase, variant) / "output.md").read_text(encoding="utf-8")
        p = self.phase_dir(phase) / "output.md"
        return p.read_text(encoding="utf-8") if p.exists() else None


def load_code(run):
    """Import the code under test. Must run before any api.* import."""
    import logging
    import os

    sys.path.insert(0, str(run.code))
    os.chdir(run.code)
    try:
        from api.services import worker as worker_mod
    except ImportError as e:
        sys.exit(
            f"cannot import the code under test ({e}). Run the harness with the repo venv: "
            "<main checkout>/venv/bin/python test-runs/harness.py …"
        )
    # The worker logs prod-style JSON (job_id 9999 is the harness's fake job); keep stdout readable.
    logging.disable(logging.INFO)
    return worker_mod


def build_context(run, w, variant=None):
    from api.services.utils import calculate_transcript_metrics

    routing = w.llm.config.get("routing", {})
    metrics = calculate_transcript_metrics(
        run.srt, long_form_threshold_minutes=routing.get("long_form_threshold_minutes", 15)
    )
    metrics = w._resolve_duration_into_metrics(metrics, run.srt_path)
    run.job["duration_minutes"] = metrics["estimated_duration_minutes"]
    run.job["word_count"] = metrics["word_count"]
    p = run.dir / "sst_context.json"
    sst = json.loads(p.read_text()) if p.exists() else None
    ctx = {
        "project_name": run.job["project_name"],
        "transcript": run.srt,
        "transcript_file": run.job["transcript_file"],
        "project_path": run.dir / "_scratch_out",
        "transcript_metrics": metrics,
        "sst_context": sst,
        "content_type": w._detect_content_type(metrics, run.srt_path, sst),
        "diarization_result": None,
    }
    for ph in PHASES:
        out = run.output(ph, variant)
        if out is not None:
            ctx[f"{ph}_output"] = out
    cc = run.dir / "formatter" / "completeness.json"
    if cc.exists():
        ctx["completeness_check"] = json.loads(cc.read_text())
    return ctx, metrics


# --------------------------------------------------------------------------- setup


def git(*args, cwd=REPO_ROOT, check=True):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)


def cmd_code(a):
    dest = Path(a.dest).resolve()
    prs = [p for p in (a.prs or "").split(",") if p]
    resuming = prs and (dest / ".git").is_file()  # an existing stack worktree: continue merging
    if dest.exists() and not resuming:
        sys.exit(f"{dest} already exists")
    git("fetch", "-q", "origin")
    base = git("rev-parse", "origin/main").stdout.strip()
    if not prs:
        dest.mkdir(parents=True)
        archive = subprocess.run(["git", "archive", "origin/main"], cwd=REPO_ROOT, capture_output=True, check=True)
        subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout, check=True)
        print(json.dumps({"code": str(dest), "base": base, "prs": []}))
        return
    # A PR stack needs real merges: use a detached worktree (never pushed, removed after the run).
    if not resuming:
        git("worktree", "add", "--detach", str(dest), "origin/main")
    merged = []
    for n in prs:
        git("fetch", "-q", "origin", f"pull/{n}/head", cwd=dest)
        head = git("rev-parse", "FETCH_HEAD", cwd=dest).stdout.strip()
        r = git("merge", "--no-edit", "-q", "FETCH_HEAD", cwd=dest, check=False)
        if r.returncode != 0:
            conflicts = git("diff", "--name-only", "--diff-filter=U", cwd=dest).stdout.split()
            print(
                json.dumps(
                    {"code": str(dest), "base": base, "merged": merged, "conflict_on": n, "files": conflicts}, indent=2
                )
            )
            sys.exit(f"merge conflict on #{n}: resolve in {dest} and `git commit`, then re-run with the remaining PRs")
        merged.append({"pr": int(n), "head": head})
    print(json.dumps({"code": str(dest), "base": base, "prs": merged}, indent=2))


def cmd_setup(a):
    d = Path(a.run_dir).resolve()
    d.mkdir(parents=True, exist_ok=True)
    src = a.srt
    name = Path(src.split("?")[0]).name
    if src.startswith("http"):
        try:
            with urllib.request.urlopen(src, timeout=60) as r:
                data = r.read()
                modified = r.headers.get("Last-Modified")
        except OSError as e:  # URLError, HTTPError and timeouts are all OSErrors
            sys.exit(f"could not download {src}: {e}")
    else:
        data = Path(src).read_bytes()
        modified = None
    (d / name).write_bytes(data)
    meta = {
        "srt": name,
        "source": src,
        "source_last_modified": modified,
        "project_name": Path(name).stem,
        "media_id": a.media_id,
        "code": str(Path(a.code).resolve()),
        "prs": [int(p) for p in (a.prs or "").split(",") if p],
    }
    (d / "run.json").write_text(json.dumps(meta, indent=2))
    ascii_only = all(b < 128 for b in data)
    music_glyph_loss = len(re.findall(r"^\?[ ?]*\?\s*$", data.decode("utf-8", "replace"), flags=re.M))
    print(json.dumps(meta, indent=2))
    print(f"bytes={len(data)} ascii_only={ascii_only} captions_that_are_only_'?'={music_glyph_loss}")


# --------------------------------------------------------------------------- SST


def cmd_sst_fields(a):
    run_dir = Path(a.run_dir).resolve() if a.run_dir else None
    code = Path(json.loads((run_dir / "run.json").read_text())["code"]) if run_dir else REPO_ROOT
    sys.path.insert(0, str(code))
    from api.services import worker as wm

    print("SST table fields (by name):", json.dumps(list(wm.SST_CONTEXT_AIRTABLE_FIELDS)))
    print("Linked Project fields (by name):", json.dumps(list(wm.SST_PROJECT_FIELD_MAP.values())))


class FakeAirtable:
    """Serves saved Airtable records to the worker's own _fetch_sst_context."""

    def __init__(self, record, project):
        self.record, self.project = record, project

    async def get_sst_record(self, record_id):
        return self.record if self.record and self.record.get("id") == record_id else None

    async def search_sst_by_media_id(self, media_id):
        return self.record

    async def get_project_record(self, record_id):
        return self.project


def cmd_sst(a):
    run = Run(a.run_dir)
    wm = load_code(run)
    record = json.loads(Path(a.record).read_text()) if a.record else None
    project = json.loads(Path(a.project).read_text()) if a.project else None
    job = dict(run.job)
    if record:
        job["airtable_record_id"] = record.get("id")
    with mock.patch.object(wm, "get_airtable_client", lambda: FakeAirtable(record, project)):
        ctx = asyncio.run(wm.JobWorker()._fetch_sst_context(job))
    out = run.dir / "sst_context.json"
    if ctx is None:
        out.unlink(missing_ok=True)
        print("SST context: None (prod would run every phase without SST)")
        return
    out.write_text(json.dumps(ctx, indent=2))
    print(
        json.dumps({k: (v[:80] + "…" if isinstance(v, str) and len(v) > 80 else v) for k, v in ctx.items()}, indent=2)
    )


# --------------------------------------------------------------------------- intake


def cmd_intake(a):
    run = Run(a.run_dir)
    wm = load_code(run)
    from api.services.chunking import split_transcript
    from api.services.completeness import count_source_words
    from api.services.speaker_segmentation import split_interior_speaker_changes
    from api.services.utils import get_srt_duration, parse_srt

    w = wm.JobWorker()
    ctx, metrics = build_context(run, w)
    caps = parse_srt(run.srt)
    split = split_interior_speaker_changes(run.srt)
    cfg = w.llm.config["routing"]["chunking"]
    backend = w.llm.get_backend_for_phase("formatter")
    max_tokens = w.llm.get_backend_config(backend).get("max_tokens")
    if "max_output_tokens" in inspect.signature(split_transcript).parameters:
        chunks = split_transcript(split, is_srt=True, config=cfg, max_output_tokens=max_tokens)
    else:
        chunks = split_transcript(split, is_srt=True, config=cfg)
    text = " ".join(c.text for c in caps)
    report = {
        "code": str(run.code),
        "job_word_count": metrics["word_count"],
        "dialogue_words": count_source_words(run.srt, True),
        "duration_min": round(get_srt_duration(caps) / 60000, 2),
        "captions": len(caps),
        "captions_after_>>_split": len(parse_srt(split)),
        "turn_markers_>>": run.srt.count(">>"),
        "plain_name_labels": len(re.findall(r"(?:^|\n)[A-Z][\w .'-]{1,40}:\s", run.srt)),
        "bracket_or_paren_cues": len(re.findall(r"[\[(][^\])]*[\])]", text)),
        "content_type": ctx["content_type"],
        "timestamp_runs": w._should_run_timestamp_phase(run.job, metrics, run.srt_path),
        "formatter_max_tokens": max_tokens,
        "chunking": "single call" if chunks is None else [c.word_count for c in chunks],
        "backends": {p: w.llm.get_backend_for_phase(p) for p in PHASES},
        "phase_models": {p: w.llm.config.get("phase_models", {}).get(p) for p in PHASES},
        "reasoning": w.llm.config.get("routing", {}).get("reasoning"),
        "sst_loaded": bool(ctx["sst_context"]),
    }
    print(json.dumps(report, indent=2))


# --------------------------------------------------------------------------- prompts


class Recorder:
    """Stands in for LLMClient.chat: records every call, returns a stub."""

    def __init__(self, LLMResponse):
        self.calls, self.LLMResponse = [], LLMResponse

    async def __call__(self, messages, backend=None, model=None, job_id=None, phase=None, **kw):
        self.calls.append({"messages": messages, "backend": backend, "model": model, "phase": phase, "kwargs": kw})
        return self.LLMResponse(
            content="(captured by harness)",
            model=model or "stub",
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            cost=0.0,
            duration_ms=0,
            backend=backend or "stub",
        )


async def _noop(*a, **k):
    return None


def cmd_prompts(a):
    run = Run(a.run_dir)
    wm = load_code(run)
    from api.services.llm import LLMResponse

    w = wm.JobWorker()
    ctx, _ = build_context(run, w, a.variant)
    ctx["project_path"].mkdir(exist_ok=True)
    if a.flags:
        ctx["_validation_flags"] = json.loads(Path(a.flags).read_text())
    if a.feedback:
        ctx["_editorial_feedback"] = a.feedback
    if a.phase == "timestamp":
        ctx["srt_content"] = run.srt
        ctx["srt_path"] = str(run.srt_path)
    rec = Recorder(LLMResponse)
    w.llm.chat = rec
    if a.force_max_tokens:
        real = w.llm.get_backend_config
        w.llm.get_backend_config = lambda b: {**real(b), "max_tokens": a.force_max_tokens}
        print(f"FORCE_MAX_TOKENS={a.force_max_tokens} (experiment, not prod config)")
    with mock.patch.object(wm, "log_event", _noop):
        asyncio.run(w._run_phase(run.job["id"], a.phase, ctx, ctx["project_path"], model_override=a.model))
    shutil.rmtree(ctx["project_path"], ignore_errors=True)
    if not rec.calls:
        sys.exit(f"no LLM call captured for {a.phase}: _run_phase returned without calling the model")
    base = run.phase_dir(a.phase, a.variant)
    multi = len(rec.calls) > 1
    for i, call in enumerate(rec.calls):
        d = base / f"chunk{i}" if multi else base
        d.mkdir(parents=True, exist_ok=True)
        system = next(m["content"] for m in call["messages"] if m["role"] == "system")
        user = next(m["content"] for m in call["messages"] if m["role"] == "user")
        (d / "system.md").write_text(system, encoding="utf-8")
        (d / "user.md").write_text(user, encoding="utf-8")
        meta = {k: call[k] for k in ("backend", "model", "phase", "kwargs")}
        (d / "call_meta.json").write_text(json.dumps(meta, indent=2, default=str))
        unfilled = [
            t for t in ("{model name you are running as}", "{the model you are running as}", "{TODAY") if t in system
        ]
        print(
            f"call {i}: backend={call['backend']} model={call['model']} kwargs={call['kwargs']} "
            f"system={len(system)} user={len(user)} unfilled={unfilled} -> {d}"
        )
    print(
        f"calls captured: {len(rec.calls)}; model=None means prod's default for this phase: "
        f"{w.llm.config['phase_models'].get(a.phase)}"
    )
    keys = ("**Proper nouns", "**Keyword candidates", "**Boundary candidates", "- (none detected")
    print("style-block lines to check (names / keywords / chapter candidates):")
    for i, call in enumerate(rec.calls):
        user = next(m["content"] for m in call["messages"] if m["role"] == "user")
        style = [line for line in user.splitlines() if line.startswith(keys)]
        label = f"  call {i}:" if multi else " "
        print(label, "(none for this phase)" if not style else "")
        for line in style:
            print("    ", line[:240])


def cmd_brief(a):
    run = Run(a.run_dir)
    d = run.phase_dir(a.phase, a.variant)
    if a.chunk is not None:
        d = d / f"chunk{a.chunk}"
    if not (d / "user.md").exists():
        sys.exit(f"no captured prompt in {d}; run `prompts` first")
    # Self-reports ask for decisions and evidence, never a log of reasoning: per Anthropic's guidance,
    # a request for reasoning "in full" is what the reasoning_extraction safeguard is there to stop.
    asks = {
        "analyst": "names, spellings, program or timings you could not confirm from the input",
        "formatter": "speaker attributions you weren't sure of (timecode, who you chose); words you changed "
        "(caption -> output); how sound cues and non-English speech were rendered; whether you added "
        "review notes or set needs_review, and the reason",
        "seo": "information you needed but did not have; anything you could not confirm from the input",
        "validator": "flags you raised, and any you considered but left out, each with the evidence",
        "timestamp": "each chapter start with the SRT caption it begins on; any boundary you were unsure of",
    }
    tier = SUBAGENT_TIER[a.phase]
    print(f"DISPATCHER: send the text between the rules as one subagent prompt, with the Agent tool's model={tier}.")
    print("-" * 72)
    print(
        f"""Do this task. In the directory below, system.md is your instructions and user.md is the input. Read both in full (use offset/limit if they are long), and read no other files.

Directory: {d}

1. Write the result the instructions ask for to output.md in that directory: the result only, with no preamble or commentary (write in parts and append if it is long). Don't use tools to count, measure or compute anything in the result.
2. Then write selfreport.md in that directory, a short list of: instructions that conflicted (quote them) and which one you followed; {asks[a.phase]}; anything an editor should check.

Reply with a 3-line summary when done."""
    )
    print("-" * 72)


def cmd_merge(a):
    run = Run(a.run_dir)
    load_code(run)
    from api.services.chunking import merge_formatter_chunks
    from api.services.speaker_segmentation import split_interior_speaker_changes

    bdir = run.phase_dir("formatter", a.variant)
    parts, i = [], 0
    while (bdir / f"chunk{i}" / "output.md").exists():
        parts.append((bdir / f"chunk{i}" / "output.md").read_text(encoding="utf-8"))
        i += 1
    if not parts:
        sys.exit(f"no chunk outputs under {bdir}")
    kwargs = {}
    from api.services.worker import JobWorker

    if "header_fields" in inspect.signature(merge_formatter_chunks).parameters and hasattr(
        JobWorker, "_chunk_header_fields"
    ):
        w = JobWorker()
        ctx, _ = build_context(run, w, a.variant)
        ctx["transcript"] = split_interior_speaker_changes(ctx["transcript"])
        kwargs["header_fields"] = w._chunk_header_fields(ctx)
        print("header_fields:", kwargs["header_fields"])
    merged = merge_formatter_chunks(parts, **kwargs)
    (bdir / "output.md").write_text(merged, encoding="utf-8")
    print(f"merged {i} chunks -> {bdir / 'output.md'} ({len(merged)} chars)")


# --------------------------------------------------------------------------- gates


def gate_analyst(run, out, w):
    from api.services.utils import get_srt_duration, parse_srt

    dur = get_srt_duration(parse_srt(run.srt)) / 1000
    secs = []
    for h, m, s in re.findall(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b", out):
        secs.append(int(h) * 3600 + int(m) * 60 + int(s) if s else int(h) * 60 + int(m))
    over = sorted({s for s in secs if s > dur + 5})
    print(f"duration {dur:.1f}s; timecodes {len(secs)}; max {max(secs) if secs else 0}s")
    print("AN-1 timings beyond duration:", over[:20] or "none", "-> FAIL" if over else "-> ok")
    print("Program line:", (re.findall(r"\*\*Program:\*\*\s*(.+)", out) or ["(none)"])[0])
    print("sections:", re.findall(r"^#{1,3} .*$", out, flags=re.M)[:30])


def gate_formatter(run, out, w, variant):
    from api.services.completeness import check_completeness, count_content_words
    from api.services.seam_coverage import DEFAULT_BLOCKING_RATIO, find_dropped_spans, format_gap_message

    cfg = w.llm.config["routing"]["completeness"]
    res = check_completeness(
        out,
        run.srt,
        is_srt=True,
        duration_minutes=run.job.get("duration_minutes"),
        threshold=cfg.get("coverage_threshold", 0.7),
        min_source_words=cfg.get("min_source_words", 500),
    )
    print("completeness:", json.dumps(res.to_dict()))
    if not variant:  # only the prod-path output feeds later phases' context
        (run.dir / "formatter" / "completeness.json").write_text(json.dumps(res.to_dict()))
    if not res.is_complete and not res.skipped:
        print("PROD WOULD PAUSE: [truncation]")
    scfg = w.llm.config["routing"]["seam_coverage"]
    seam = find_dropped_spans(
        source_transcript=run.srt,
        formatter_output=out,
        is_srt=True,
        min_run=scfg.get("min_run", 4),
        per_caption_floor=scfg.get("per_caption_floor", 0.5),
        blocking_ratio=scfg.get("blocking_ratio", DEFAULT_BLOCKING_RATIO),
    )
    print("seam:", json.dumps({k: v for k, v in seam.to_dict().items() if k != "dropped_spans"}, default=str))
    if seam.has_gap and seam.blocking:
        print("PROD WOULD PAUSE: [seam]", format_gap_message(seam))
    low = out.lower()
    markers = [
        "# formatted transcript",
        "<!-- review notes",
        "status:** needs_review",
        "status:** ready_for_editing",
        "section 1 of",
        "speaker 1",
        "unidentified",
    ]
    print("contract markers:", {m: (m in low) for m in markers})
    print("header:", dict(re.findall(r"\*\*(Project|Program|Duration|Date Processed):\*\*\s*(.+)", out)))
    labels = Counter(re.findall(r"^\*\*([^*]+?):\*\*", out, flags=re.M))
    for k in ("Project", "Program", "Duration", "Date Processed", "Status"):
        labels.pop(k, None)
    print("speaker labels:", dict(labels), "| source >> markers:", run.srt.count(">>"))
    print("content words:", count_content_words(out))


def _section(text, heading):
    m = re.search(rf"^#+\s*{heading}.*?$(.*?)(?=^#+\s|\Z)", text, flags=re.M | re.S | re.I)
    return m.group(1) if m else ""


def gate_seo(run, out, w):
    for field, lim in {"title": 80, "short description": 90, "long description": 350}.items():
        body = _section(out, field)
        m = re.search(r"\*\*Recommended:\*\*\s*\n(.+)", body)
        value = (m.group(1) if m else "").strip().strip("`")
        flag = "OVER" if len(value) > lim else ("MISSING" if not value else "ok")
        print(f"{field} [{len(value)}/{lim}] {flag}: {value}")
    print(
        "drafting-trail markers:",
        re.findall(r"exceeds limit|revised below|Final revision|\(\d+ char(?!s max\))[^)]*\)", out, re.I)[:10],
    )
    print(
        "status:",
        re.findall(r"\*\*Status:\*\*\s*(\S+)", out),
        "| program:",
        re.findall(r"\*\*Program:\*\*\s*(.+)", out),
    )


def gate_timestamp(run, out, w):
    from api.services.utils import parse_srt

    caps = parse_srt(run.srt)
    starts = {c.start_ms for c in caps}
    end_ms = max(c.end_ms for c in caps)
    rows = re.findall(
        r"^\|\s*([^|]+?)\s*\|\s*(\d+:\d{2}:\d{2}\.\d{3})\s*\|\s*(\d+:\d{2}:\d{2}\.\d{3})\s*\|", out, flags=re.M
    )
    rows = [r for r in rows if r[0].lower() != "title"]

    def ms(t):
        h, m, s = t.split(":")
        return int((int(h) * 3600 + int(m) * 60 + float(s)) * 1000)

    print(f"chapters: {len(rows)}; SRT end {end_ms / 1000:.3f}s")
    for title, start, stop in rows:
        on = ms(start) == 0 or ms(start) in starts
        print(
            f"  {start}  {title!r:52} {'on caption start' if on else 'NOT a caption start'}  words={len(title.split())}"
        )
    if rows:
        print(f"final end {rows[-1][2]} vs SRT end {end_ms / 1000:.3f}s")
    if len(rows) < 2 and end_ms > 10 * 60000:
        print("TS FAIL: fewer than 2 chapters on a >= 10-minute piece (see #449: empty boundary-candidate list)")


def cmd_gate(a):
    run = Run(a.run_dir)
    wm = load_code(run)
    w = wm.JobWorker()
    build_context(run, w, a.variant)
    out = run.output(a.phase, a.variant)
    if out is None:
        sys.exit(f"no output.md for {a.phase} {a.variant or ''}")
    if a.phase == "validator":
        gate_validator(run, out, w, wm, a.variant)
    elif a.phase == "formatter":
        gate_formatter(run, out, w, a.variant)
    elif a.phase == "analyst":
        gate_analyst(run, out, w)
    elif a.phase == "seo":
        gate_seo(run, out, w)
    elif a.phase == "timestamp":
        gate_timestamp(run, out, w)


def gate_validator(run, out, w, wm, variant):
    from api.services.escalation import classify_qa_failure, nonfixable_review_message, select_escalation_phases

    ctx, _ = build_context(run, w, variant)
    try:
        verdict = wm.JobWorker._parse_validation_result(out)
    except Exception as e:
        print("JSON PARSE FAILED:", e, "-> prod stores no verdict")
        return
    with mock.patch.object(wm, "log_event", _noop):
        try:
            verdict = asyncio.run(w._apply_style_lint(run.job["id"], ctx, verdict))
        except Exception as e:
            print("style lint raised:", e)
    print("verdict:", json.dumps(verdict, indent=2))
    if verdict.get("overall") != "fail":
        print("QA gate: COMPLETED")
        return
    classed = classify_qa_failure(verdict, ctx)
    print("classify:", json.dumps(classed, indent=2, default=str))
    if not classed["escalate"]:
        print("PROD WOULD PAUSE: [qa_review]", nonfixable_review_message(classed["nonfixable"]))
    else:
        phases = [p for p in select_escalation_phases(verdict, list(wm.JobWorker.PHASES)) if p != "validator"]
        print("PROD WOULD ESCALATE:", {p: ESCALATION[SUBAGENT_TIER[p]] for p in phases})
        print(
            "Note: prod's escalation context carries no validator flags (#450); capture escalated prompts WITHOUT --flags."
        )


# --------------------------------------------------------------------------- OpenRouter fallback

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
REASONING_MANDATORY = "reasoning is mandatory"


def _openrouter_key():
    r = subprocess.run(["get-secret.sh", "OPENROUTER_API_KEY"], capture_output=True, text=True)
    key = r.stdout.strip()
    if r.returncode != 0 or not key:
        sys.exit("could not resolve OPENROUTER_API_KEY via get-secret.sh (is it on PATH?)")
    return key


def cmd_replay(a):
    """Send a captured prompt to OpenRouter unchanged: the fallback when a subagent can't run a phase.

    It sends exactly what the code under test passed to ``llm.chat`` for this call: the captured
    messages, the backend's ``max_tokens``, and a ``reasoning`` kwarg only if the worker passed one
    (plain main passes none; #409 adds ``{"enabled": False}`` for the formatter). Other kwargs are
    not forwarded and are listed in ``notes``.
    """
    import os
    import time

    import httpx

    run = Run(a.run_dir)
    d = run.phase_dir(a.phase, a.variant)
    if a.chunk is not None:
        d = d / f"chunk{a.chunk}"
    if not (d / "user.md").exists():
        sys.exit(f"no captured prompt in {d}; run `prompts` first")
    system = (d / "system.md").read_text(encoding="utf-8")
    user = (d / "user.md").read_text(encoding="utf-8")
    meta = json.loads((d / "call_meta.json").read_text())
    kwargs = meta.get("kwargs") or {}

    wm = load_code(run)
    w = wm.JobWorker()
    key = _openrouter_key()
    model = a.model or meta.get("model") or w.llm.config.get("phase_models", {}).get(a.phase)
    if not model:
        sys.exit(f"no model for {a.phase}: none captured and no phase_models entry; pass --model")
    if a.newest:
        from api.services import model_roster

        family = next((f for f in ("haiku", "sonnet", "opus") if f in model), None)
        if not family:
            sys.exit(f"--newest: can't tell the family of {model!r}; pass --model")
        os.environ["OPENROUTER_API_KEY"] = key
        model = asyncio.run(model_roster.newest_in_family(family, ["fast", "fable"]))
        if not model:
            sys.exit(f"model roster returned nothing for {family}; pass --model explicitly")
    max_source = "--max-tokens"
    max_tokens = a.max_tokens
    if not max_tokens:
        try:
            max_tokens = w.llm.get_backend_config(meta.get("backend") or "openrouter").get("max_tokens")
            max_source = f"backend {meta.get('backend')!r} config"
        except Exception as e:
            max_source = f"fallback 16384 (backend config unreadable: {e})"
        if not max_tokens:
            max_tokens, max_source = 16384, "fallback 16384 (backend sets no max_tokens)"
    print(f"model={model} max_tokens={max_tokens} ({max_source})")

    notes = []
    reasoning = kwargs.get("reasoning")
    if reasoning is None:
        notes.append("no reasoning control captured: the code under test sends none, so the model default applies")
        print("NOTE:", notes[-1])
    dropped = sorted(k for k in kwargs if k != "reasoning")
    if dropped:
        notes.append(f"captured kwargs not forwarded: {dropped}")

    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "usage": {"include": True},
    }
    if reasoning is not None:
        payload["reasoning"] = reasoning
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://pbswisconsin.org",
        "X-Title": "Cardigan test-run",
    }
    out = d / "replay"
    out.mkdir(exist_ok=True)
    for stale in ("output.md", "telemetry.json"):  # never leave a previous run's files to be promoted
        (out / stale).unlink(missing_ok=True)

    def post():
        try:
            return httpx.post(OPENROUTER_URL, json=payload, headers=headers, timeout=900)
        except httpx.HTTPError as e:
            sys.exit(f"OpenRouter request failed ({type(e).__name__}: {e}); nothing was saved")

    t0 = time.time()
    r = post()
    if r.status_code == 400 and REASONING_MANDATORY in r.text.lower() and "reasoning" in payload:
        # Model capability, not content: some models reject reasoning control. Record it and resend
        # the identical prompt without the control. The prompt itself is never changed.
        (out / f"raw_{time.strftime('%H%M%S')}_400.json").write_text(r.text, encoding="utf-8")
        notes.append(
            f"{model} rejected reasoning={reasoning}; resent with reasoning ON (prod's control not replayable)"
        )
        payload.pop("reasoning")
        r = post()
    elapsed = round(time.time() - t0, 1)
    raw = out / f"raw_{time.strftime('%H%M%S')}.json"
    raw.write_text(r.text, encoding="utf-8")
    if r.status_code != 200:
        sys.exit(f"HTTP {r.status_code}; body saved to {raw}")
    data = r.json()
    choice = data["choices"][0]
    content = choice["message"].get("content")
    usage = data.get("usage") or {}
    reasoning_tokens = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
    tel = {
        "backend": "openrouter",
        "model_requested": model,
        "model_actual": data.get("model"),
        "provider": data.get("provider"),
        "finish_reason": choice.get("finish_reason"),
        "native_finish_reason": choice.get("native_finish_reason"),
        "max_tokens_sent": max_tokens,
        "max_tokens_source": max_source,
        "reasoning_sent": payload.get("reasoning"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": reasoning_tokens,
        "headroom_tokens": max_tokens - (usage.get("completion_tokens") or 0),
        "cost_usd": usage.get("cost", usage.get("total_cost")),
        "elapsed_s": elapsed,
        "content_null": content is None,
        "raw": raw.name,
        "notes": notes,
    }
    if content is not None:
        (out / "output.md").write_text(content, encoding="utf-8")
    (out / "telemetry.json").write_text(json.dumps(tel, indent=2))
    print(json.dumps(tel, indent=2))
    if content is None:
        sys.exit(f"no content returned (finish_reason={choice.get('finish_reason')}); raw saved to {raw}")
    if tel["finish_reason"] == "length":
        print("WARNING: finish_reason=length — output truncated at max_tokens; prod would raise/pause (#403)")
    print(f"saved to {out / 'output.md'}; run `promote` to make it this phase's output")


def cmd_promote(a):
    """Make a phase's OpenRouter replay its output (no network), so gates and later phases use it."""
    run = Run(a.run_dir)
    d = run.phase_dir(a.phase, a.variant)
    if a.chunk is not None:
        d = d / f"chunk{a.chunk}"
    src, tel_path = d / "replay" / "output.md", d / "replay" / "telemetry.json"
    if not src.exists() or not tel_path.exists():
        sys.exit(f"no complete replay in {d / 'replay'}; run `replay` first")
    tel = json.loads(tel_path.read_text())
    if tel.get("finish_reason") != "stop" and not a.force:
        sys.exit(
            f"replay finish_reason={tel.get('finish_reason')!r}, not 'stop'; refusing (use --force to promote anyway)"
        )
    (d / "output.md").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    keep = ("backend", "model_actual", "finish_reason", "reasoning_sent", "notes")
    (d / "backend.json").write_text(json.dumps({k: tel.get(k) for k in keep}, indent=2))
    print(f"promoted {src} -> {d / 'output.md'} (backend.json records openrouter / {tel.get('model_actual')})")


def cmd_index(a):
    run = Run(a.run_dir)
    row = {
        "unit": run.dir.name,
        "kind": "manual-supervisor-run",
        "media_id": run.meta.get("media_id"),
        "transcript_file": run.meta["srt"],
        "source": run.meta["source"],
        "prs": run.meta.get("prs"),
    }
    for ph in PHASES:
        row[f"{ph}_output"] = (run.phase_dir(ph) / "output.md").exists()
    cc = run.dir / "formatter" / "completeness.json"
    if cc.exists():
        row["formatter_coverage"] = json.loads(cc.read_text()).get("coverage_ratio")
    print(json.dumps(row, indent=2))


# --------------------------------------------------------------------------- CLI


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    s = p.add_subparsers(dest="cmd", required=True)

    def sub(name, help_, run_dir=True):
        c = s.add_parser(name, help=help_, description=help_)
        if run_dir:
            c.add_argument("--run-dir", required=True, help="unit directory (holds run.json)")
        return c

    variant = {"help": "variant subdir, e.g. esc, supervised, forced-chunk"}
    c = sub("code", "export origin/main, or build main + PRs in a detached worktree", run_dir=False)
    c.add_argument("--dest", required=True, help="new directory for the code tree")
    c.add_argument("--prs", help="comma-separated PR numbers to merge (re-run to resume after a conflict)")
    c = sub("setup", "create a unit: fetch the SRT and write run.json")
    c.add_argument("--srt", required=True, help="URL or local path")
    c.add_argument("--code", required=True, help="code tree from `code`")
    c.add_argument("--media-id", help="drives the SST lookup; omit if unknown")
    c.add_argument("--prs", help="recorded in run.json for the log")
    c = sub("sst-fields", "print the Airtable field names to fetch", run_dir=False)
    c.add_argument("--run-dir", help="optional: use this unit's code tree")
    c = sub("sst", "build sst_context.json through the worker's _fetch_sst_context")
    c.add_argument("--record", help='SST record JSON {"id": ..., "fields": {name: value}}; omit if none')
    c.add_argument("--project", help="linked Project record JSON, same shape")
    sub("intake", "metrics, caption convention, chunking decision, models, SST status")
    c = sub("prompts", "capture the prompt(s) prod would send for a phase")
    c.add_argument("phase", choices=PHASES)
    c.add_argument("--variant", **variant)
    c.add_argument("--flags", help="JSON list of validator flags (operator-retry emulation only)")
    c.add_argument("--feedback", help="editorial feedback text (operator-retry emulation only)")
    c.add_argument("--model", help="model override, e.g. for escalation")
    c.add_argument("--force-max-tokens", type=int, help="experiment: lower the formatter cap to force chunking")
    c = sub("brief", "print the subagent dispatch prompt for a captured phase")
    c.add_argument("phase", choices=PHASES)
    c.add_argument("--variant", **variant)
    c.add_argument("--chunk", type=int, help="chunk index for a chunked formatter")
    c = sub("merge", "merge chunkN/output.md with prod's merge_formatter_chunks")
    c.add_argument("--variant", **variant)
    c = sub("gate", "run prod's gates (and the harness's checks) on a phase output")
    c.add_argument("phase", choices=PHASES)
    c.add_argument("--variant", **variant)
    c = sub("replay", "fallback: send a captured prompt, unchanged, to OpenRouter (costs money)")
    c.add_argument("phase", choices=PHASES)
    c.add_argument("--variant", **variant)
    c.add_argument("--chunk", type=int, help="chunk index for a chunked formatter")
    c.add_argument("--model", help="explicit OpenRouter model id (default: the captured/prod model)")
    c.add_argument("--newest", action="store_true", help="newest model in the same family, via prod's roster")
    c.add_argument("--max-tokens", type=int, help="override prod's backend max_tokens")
    c = sub("promote", "make a phase's replay/output.md its output.md (no network)")
    c.add_argument("phase", choices=PHASES)
    c.add_argument("--variant", **variant)
    c.add_argument("--chunk", type=int, help="chunk index for a chunked formatter")
    c.add_argument("--force", action="store_true", help="promote even if finish_reason is not 'stop'")
    sub("index", "print an index row for the private cohort")
    a = p.parse_args()
    # Resolve every local path against the caller's cwd now: load_code() later chdirs into the
    # code tree, after which a relative --record / --project / --flags would resolve there.
    for attr in ("run_dir", "record", "project", "flags", "dest", "code"):
        if getattr(a, attr, None):
            setattr(a, attr, str(Path(getattr(a, attr)).resolve()))
    if getattr(a, "srt", None) and not a.srt.startswith(("http://", "https://")):
        a.srt = str(Path(a.srt).resolve())
    {
        "code": cmd_code,
        "setup": cmd_setup,
        "sst-fields": cmd_sst_fields,
        "sst": cmd_sst,
        "intake": cmd_intake,
        "prompts": cmd_prompts,
        "brief": cmd_brief,
        "merge": cmd_merge,
        "gate": cmd_gate,
        "replay": cmd_replay,
        "promote": cmd_promote,
        "index": cmd_index,
    }[a.cmd](a)


if __name__ == "__main__":
    main()
