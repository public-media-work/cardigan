"""Supervised test-run harness for Cardigan (used by the /test-run skill).

Drives one caption file through prod's own prompt builders and gates, with the model
calls made by local subagents. Every prompt is CAPTURED, not re-implemented: the
harness runs the worker's real ``_run_phase`` (and through it
``_run_formatter_chunked``) with ``llm.chat`` replaced by a recorder, so the files it
writes are exactly what the code under test would send. Prompt capture and gates make
no LLM, network or DB calls; only ``code`` (git fetch) and ``setup`` (SRT download) touch
the network.

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
            "~/Developer/cardigan/venv/bin/python test-runs/harness.py …"
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
        with urllib.request.urlopen(src) as r:
            data = r.read()
            modified = r.headers.get("Last-Modified")
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
    style = [line for line in user.splitlines() if line.startswith(keys)]
    print(
        "style-block lines to check (names / keywords / chapter candidates):",
        "(none for this phase)" if not style else "",
    )
    for line in style:
        print("  ", line[:240])


def cmd_brief(a):
    run = Run(a.run_dir)
    d = run.phase_dir(a.phase, a.variant)
    if a.chunk is not None:
        d = d / f"chunk{a.chunk}"
    if not (d / "user.md").exists():
        sys.exit(f"no captured prompt in {d}; run `prompts` first")
    asks = {
        "analyst": "anything you had to guess (speaker identities, spellings, program name, timings)",
        "formatter": "every speaker attribution you were unsure of (timecode, who, why), every word you added, "
        "removed or changed relative to the captions, how you handled sound cues and non-English "
        "speech, and whether you added review notes or set needs_review and why",
        "seo": "what information you felt was missing, how you counted characters, and anything you guessed",
        "validator": "for each flag you raised, or considered and did not raise, why",
        "timestamp": "how you chose each chapter boundary (with the SRT caption it starts on) and anything you guessed",
    }
    tier = SUBAGENT_TIER[a.phase]
    print(f"DISPATCHER: send the text between the rules as one subagent prompt, with the Agent tool's model={tier}.")
    print("-" * 72)
    print(
        f"""You are acting as one LLM call inside a content-processing pipeline. Read only the two input files named below; write only the two output files named below.

Directory: {d}

1. Read system.md in that directory. Treat it as your system prompt.
2. Read user.md in that directory. Treat it as the user message. Read all of it (use offset/limit if it is long).
3. Produce exactly the response that system prompt asks for. Write ONLY that response, with no preamble or commentary, to output.md in that directory (write in parts and append if it is long).
4. Then write selfreport.md in that directory: a bullet list of instructions that conflicted or were ambiguous and how you resolved each, plus {asks[a.phase]}. Be candid.

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
        return gate_validator(run, out, w, wm, a.variant)
    {"analyst": gate_analyst, "seo": gate_seo, "timestamp": gate_timestamp}.get(a.phase, None) and {
        "analyst": gate_analyst,
        "seo": gate_seo,
        "timestamp": gate_timestamp,
    }[a.phase](run, out, w)
    if a.phase == "formatter":
        gate_formatter(run, out, w, a.variant)


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
    c = s.add_parser("code")
    c.add_argument("--dest", required=True)
    c.add_argument("--prs")  # noqa: E702
    c = s.add_parser("setup")
    c.add_argument("--run-dir", required=True)
    c.add_argument("--srt", required=True)  # noqa: E702
    c.add_argument("--code", required=True)
    c.add_argument("--media-id")
    c.add_argument("--prs")  # noqa: E702
    c = s.add_parser("sst-fields")
    c.add_argument("--run-dir")  # noqa: E702
    c = s.add_parser("sst")
    c.add_argument("--run-dir", required=True)  # noqa: E702
    c.add_argument("--record")
    c.add_argument("--project")  # noqa: E702
    c = s.add_parser("intake")
    c.add_argument("--run-dir", required=True)  # noqa: E702
    c = s.add_parser("prompts")
    c.add_argument("--run-dir", required=True)
    c.add_argument("phase", choices=PHASES)  # noqa: E702
    c.add_argument("--variant")
    c.add_argument("--flags")
    c.add_argument("--feedback")  # noqa: E702
    c.add_argument("--model")
    c.add_argument("--force-max-tokens", type=int)  # noqa: E702
    c = s.add_parser("brief")
    c.add_argument("--run-dir", required=True)
    c.add_argument("phase", choices=PHASES)  # noqa: E702
    c.add_argument("--variant")
    c.add_argument("--chunk", type=int)  # noqa: E702
    c = s.add_parser("merge")
    c.add_argument("--run-dir", required=True)
    c.add_argument("--variant")  # noqa: E702
    c = s.add_parser("gate")
    c.add_argument("--run-dir", required=True)
    c.add_argument("phase", choices=PHASES)  # noqa: E702
    c.add_argument("--variant")
    c = s.add_parser("index")
    c.add_argument("--run-dir", required=True)  # noqa: E702
    a = p.parse_args()
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
        "index": cmd_index,
    }[a.cmd](a)


if __name__ == "__main__":
    main()
