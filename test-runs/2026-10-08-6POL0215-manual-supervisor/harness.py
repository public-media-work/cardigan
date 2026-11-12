"""Manual-supervisor harness, 6POL0215 run. It is a generalised version of the 6POL0213
harness, and the main difference is how prompts are built. The 6POL0213 harness
re-implemented the chunk prompts. This one CAPTURES them by running the worker's own
``_run_phase`` / ``_run_formatter_chunked`` with ``llm.chat`` replaced by a recorder,
so whatever the code under test would send is exactly what gets written. No network,
no DB, read-only against the code tree.

Environment:
  CODE=<path>   code tree to import (a `git archive` export of the stack or of main)
  ESC=<subdir>  variant subdir (esc, final, ...): reads <phase>/<ESC>/output.md first,
                writes prompts to <phase>/<ESC>/

Usage:
  harness.py intake
  harness.py prompts <phase> [--flags flags.json] [--model <override>] [--feedback text]
  harness.py merge [base_dir]            -> real merge_formatter_chunks (+ header_fields when present)
  harness.py gate formatter [output.md]  -> completeness + seam + contract markers
  harness.py gate seo [output.md]        -> measured field lengths vs limits
  harness.py gate analyst [output.md]    -> act timings vs SRT duration (AN-1)
  harness.py gate validator [output.md]  -> parse + style lint + QA-gate routing
"""

import asyncio
import inspect
import json
import os
import re
import sys
from pathlib import Path
from unittest import mock

CODE = Path(os.environ["CODE"]).resolve()
RUN = Path(__file__).resolve().parent
sys.path.insert(0, str(CODE))
os.chdir(CODE)

from api.services import worker as worker_mod  # noqa: E402
from api.services.chunking import split_transcript  # noqa: E402
from api.services.completeness import check_completeness, count_content_words, count_source_words  # noqa: E402
from api.services.escalation import (  # noqa: E402
    classify_qa_failure,
    nonfixable_review_message,
    select_escalation_phases,
)
from api.services.llm import LLMResponse  # noqa: E402
from api.services.seam_coverage import DEFAULT_BLOCKING_RATIO, find_dropped_spans, format_gap_message  # noqa: E402
from api.services.utils import calculate_transcript_metrics, get_srt_duration, parse_srt  # noqa: E402
from api.services.worker import JobWorker  # noqa: E402

MEDIA_ID = "6POL0215"
SRT_PATH = RUN / f"{MEDIA_ID}.srt"
SRT = SRT_PATH.read_text(encoding="utf-8")
JOB = {"id": 9999, "project_name": MEDIA_ID, "transcript_file": f"{MEDIA_ID}.srt", "media_id": MEDIA_ID}
PHASE_ORDER = list(JobWorker.PHASES)
ESC = os.environ.get("ESC")


def phase_output(phase):
    if ESC and (RUN / phase / ESC / "output.md").exists():
        return (RUN / phase / ESC / "output.md").read_text(encoding="utf-8")
    p = RUN / phase / "output.md"
    return p.read_text(encoding="utf-8") if p.exists() else None


def build_context(w):
    routing = w.llm.config.get("routing", {})
    metrics = calculate_transcript_metrics(
        SRT, long_form_threshold_minutes=routing.get("long_form_threshold_minutes", 15)
    )
    metrics = w._resolve_duration_into_metrics(metrics, SRT_PATH)
    JOB["duration_minutes"] = metrics["estimated_duration_minutes"]
    JOB["word_count"] = metrics["word_count"]
    p = RUN / "sst_context.json"
    sst = json.loads(p.read_text()) if p.exists() else None
    content_type = w._detect_content_type(metrics, SRT_PATH, sst)
    ctx = {
        "project_name": JOB["project_name"],
        "transcript": SRT,
        "transcript_file": JOB["transcript_file"],
        "project_path": RUN / "_scratch_out",
        "transcript_metrics": metrics,
        "sst_context": sst,
        "content_type": content_type,
        "diarization_result": None,
    }
    for ph in PHASE_ORDER + ["timestamp"]:
        out = phase_output(ph)
        if out is not None:
            ctx[f"{ph}_output"] = out
    cc = RUN / "formatter" / "completeness.json"
    if cc.exists():
        ctx["completeness_check"] = json.loads(cc.read_text())
    return ctx, metrics


def formatter_max_tokens(w):
    backend = w.llm.get_backend_for_phase("formatter")
    try:
        return w.llm.get_backend_config(backend).get("max_tokens")
    except Exception:
        return None


def chunk_decision(w, transcript):
    cfg = w.llm.config["routing"]["chunking"]
    if "max_output_tokens" in inspect.signature(split_transcript).parameters:
        mt = formatter_max_tokens(w) or getattr(worker_mod, "DEFAULT_MAX_TOKENS", None)
        return split_transcript(transcript, is_srt=True, config=cfg, max_output_tokens=mt)
    return split_transcript(transcript, is_srt=True, config=cfg)


def cmd_intake():
    w = JobWorker()
    ctx, metrics = build_context(w)
    print("code tree:", CODE)
    print("== calculate_transcript_metrics ==")
    print(json.dumps(metrics, indent=2, default=str))
    caps = parse_srt(SRT)
    print("captions:", len(caps), "srt duration min:", round(get_srt_duration(caps) / 60000, 2))
    print("dialogue words (count_source_words is_srt):", count_source_words(SRT, True))
    print("content_type:", ctx["content_type"])
    print("timestamp phase should run:", w._should_run_timestamp_phase(JOB, metrics, SRT_PATH))
    from api.services.speaker_segmentation import split_interior_speaker_changes

    split = split_interior_speaker_changes(SRT)
    print("captions after >> split:", len(parse_srt(split)))
    print("chunking config:", json.dumps(w.llm.config["routing"]["chunking"]))
    print("formatter backend max_tokens:", formatter_max_tokens(w))
    chunks = chunk_decision(w, split)
    print(
        "chunking decision:",
        "SINGLE CALL" if chunks is None else f"{len(chunks)} chunks {[c.word_count for c in chunks]}",
    )
    print("backends:", {p: w.llm.get_backend_for_phase(p) for p in PHASE_ORDER + ["timestamp"]})
    print("phase_models:", w.llm.config.get("phase_models"))
    print("reasoning:", w.llm.config.get("routing", {}).get("reasoning"))
    print("qa_escalation:", w.llm.config.get("qa_escalation"))
    print("sst_context loaded:", bool(ctx["sst_context"]))


class Recorder:
    """Stands in for LLMClient.chat: records every call and returns a stub response."""

    def __init__(self):
        self.calls = []

    async def __call__(self, messages, backend=None, model=None, job_id=None, phase=None, **kw):
        self.calls.append({"messages": messages, "backend": backend, "model": model, "phase": phase, "kwargs": kw})
        return LLMResponse(
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


def cmd_prompts(phase, flags_file=None, model=None, feedback=None):
    w = JobWorker()
    ctx, _ = build_context(w)
    ctx["project_path"].mkdir(exist_ok=True)
    if flags_file:
        ctx["_validation_flags"] = json.loads(Path(flags_file).read_text())
    if feedback:
        ctx["_editorial_feedback"] = feedback
    if phase == "timestamp":
        ctx["srt_content"] = SRT
        ctx["srt_path"] = str(SRT_PATH)
    rec = Recorder()
    w.llm.chat = rec
    force = os.environ.get("FORCE_MAX_TOKENS")  # supplementary: simulate a smaller output cap to force chunking
    if force:
        real_cfg = w.llm.get_backend_config

        def capped(backend):
            return {**real_cfg(backend), "max_tokens": int(force)}

        w.llm.get_backend_config = capped
        print(f"FORCE_MAX_TOKENS={force} (supplementary experiment, not prod config)")
    patches = [mock.patch.object(worker_mod, name, _noop) for name in ("log_event",) if hasattr(worker_mod, name)]
    for p in patches:
        p.start()
    try:
        res = asyncio.run(w._run_phase(JOB["id"], phase, ctx, ctx["project_path"], model_override=model))
    finally:
        for p in patches:
            p.stop()
    print("run_phase result keys:", sorted(res.keys()) if isinstance(res, dict) else res)
    base = RUN / phase / ESC if ESC else RUN / phase
    multi = len(rec.calls) > 1
    for i, call in enumerate(rec.calls):
        d = base / f"chunk{i}" if multi else base
        d.mkdir(parents=True, exist_ok=True)
        sysmsg = next(m["content"] for m in call["messages"] if m["role"] == "system")
        usr = next(m["content"] for m in call["messages"] if m["role"] == "user")
        (d / "system.md").write_text(sysmsg, encoding="utf-8")
        (d / "user.md").write_text(usr, encoding="utf-8")
        meta = {k: call[k] for k in ("backend", "model", "phase", "kwargs")}
        (d / "call_meta.json").write_text(json.dumps(meta, indent=2, default=str))
        print(
            f"call {i}: backend={call['backend']} model={call['model']} kwargs={call['kwargs']} "
            f"system={len(sysmsg)} user={len(usr)} -> {d}"
        )
        print(
            "  unfilled placeholders:",
            [t for t in ["{model name you are running as}", "{the model you are running as}", "{TODAY"] if t in sysmsg],
        )
    print("default model:", w.llm.config["phase_models"].get(phase), "| calls captured:", len(rec.calls))


def cmd_merge(base="formatter"):
    from api.services.chunking import merge_formatter_chunks

    bdir = RUN / base
    parts = []
    i = 0
    while (bdir / f"chunk{i}" / "output.md").exists():
        parts.append((bdir / f"chunk{i}" / "output.md").read_text(encoding="utf-8"))
        i += 1
    kwargs = {}
    if "header_fields" in inspect.signature(merge_formatter_chunks).parameters and hasattr(
        JobWorker, "_chunk_header_fields"
    ):
        w = JobWorker()
        ctx, _ = build_context(w)
        from api.services.speaker_segmentation import split_interior_speaker_changes

        ctx["transcript"] = split_interior_speaker_changes(ctx["transcript"])
        kwargs["header_fields"] = w._chunk_header_fields(ctx)
        print("header_fields:", kwargs["header_fields"])
    merged = merge_formatter_chunks(parts, **kwargs)
    (bdir / "output.md").write_text(merged, encoding="utf-8")
    print(f"merged {i} chunks -> {bdir}/output.md ({len(merged)} chars)")
    print("=== merged head ===\n" + merged[:900])
    print("=== merged tail ===\n" + merged[-400:])


def cmd_gate_formatter(out_file=None):
    w = JobWorker()
    build_context(w)
    out = Path(out_file).read_text() if out_file else phase_output("formatter")
    cfg = w.llm.config["routing"]["completeness"]
    res = check_completeness(
        out,
        SRT,
        is_srt=True,
        duration_minutes=JOB.get("duration_minutes"),
        threshold=cfg.get("coverage_threshold", 0.7),
        min_source_words=cfg.get("min_source_words", 500),
    )
    print("== completeness ==", json.dumps(res.to_dict(), indent=2))
    (RUN / "formatter" / "completeness.json").write_text(json.dumps(res.to_dict()))
    if not res.is_complete and not res.skipped:
        print(
            "PROD WOULD PAUSE: [truncation]",
            (
                w._truncation_message(
                    coverage=res.coverage_ratio,
                    out_words=res.output_word_count,
                    src_words=res.source_word_count,
                    attempts=1,
                )
                if hasattr(w, "_truncation_message")
                else ""
            ),
        )
    scfg = w.llm.config["routing"]["seam_coverage"]
    seam = find_dropped_spans(
        source_transcript=SRT,
        formatter_output=out,
        is_srt=True,
        min_run=scfg.get("min_run", 4),
        per_caption_floor=scfg.get("per_caption_floor", 0.5),
        blocking_ratio=scfg.get("blocking_ratio", DEFAULT_BLOCKING_RATIO),
    )
    print("== seam ==", json.dumps(seam.to_dict(), indent=2, default=str)[:3000])
    if seam.has_gap:
        print("SEAM GAP", "BLOCKING -> PROD WOULD PAUSE:" if seam.blocking else "(non-blocking, recorded only)")
        if seam.blocking:
            print(format_gap_message(seam))
    low = out.lower()
    print(
        "contract markers:",
        {
            m: (m in low)
            for m in [
                "# formatted transcript",
                "<!-- review notes",
                "status:** needs_review",
                "status:** ready_for_editing",
                "section 1 of",
                "speaker 1",
                "unidentified",
            ]
        },
    )
    labels = re.findall(r"^\*\*([^*]+?):\*\*", out, flags=re.M)
    from collections import Counter

    print("speaker turns:", len(labels), dict(Counter(labels)))
    print(">> markers in SRT:", SRT.count(">>"))
    print("content words:", count_content_words(out))


def _section(text, heading):
    m = re.search(rf"^#+\s*{heading}.*?$(.*?)(?=^#+\s|\Z)", text, flags=re.M | re.S | re.I)
    return m.group(1) if m else ""


def cmd_gate_seo(out_file=None):
    out = Path(out_file).read_text() if out_file else phase_output("seo")
    limits = {"title": 80, "short description": 90, "long description": 350}
    print("SEO output", len(out), "chars")
    for field, lim in limits.items():
        body = _section(out, field)
        cands = [
            ln.strip().strip("`").strip()
            for ln in body.splitlines()
            if ln.strip() and not ln.strip().startswith(("-", "*", "|", ">", "#"))
        ]
        cands = [c for c in cands if not re.match(r"^(\(|character|chars|count|limit|source|rationale|note)", c, re.I)]
        print(f"== {field} (limit {lim}) ==")
        for c in cands[:4]:
            print(f"  [{len(c)}] {'OVER' if len(c) > lim else 'ok  '} {c[:160]}")
    trail = re.findall(r"\(\s*\d+\s*char[^)]*\)|exceeds limit|revised below|Final revision", out, re.I)
    print("drafting-trail markers:", trail[:10])
    status = re.findall(r"\*\*Status:\*\*\s*(\S+)", out)
    print("status:", status)


def cmd_gate_analyst(out_file=None):
    out = Path(out_file).read_text() if out_file else phase_output("analyst")
    dur_s = get_srt_duration(parse_srt(SRT)) / 1000
    stamps = re.findall(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b", out)
    secs = []
    for a, b, c in stamps:
        secs.append(int(a) * 3600 + int(b) * 60 + int(c) if c else int(a) * 60 + int(b))
    mx = max(secs) if secs else 0
    print(f"SRT duration {dur_s:.1f}s ({dur_s / 60:.2f} min); timecodes in analyst: {len(secs)}; max {mx}s")
    over = sorted({s for s in secs if s > dur_s + 5})
    print("AN-1 timings beyond duration:", over[:20], "-> FAIL" if over else "-> ok")
    print("words:", len(out.split()), "| sections:", re.findall(r"^#{1,3} .*$", out, flags=re.M)[:30])


def cmd_gate_validator(out_file=None):
    w = JobWorker()
    ctx, _ = build_context(w)
    out = Path(out_file).read_text() if out_file else phase_output("validator")
    try:
        verdict = JobWorker._parse_validation_result(out)
    except Exception as e:
        print("JSON PARSE FAILED:", e, "-> prod stores no verdict")
        return
    with mock.patch.object(worker_mod, "log_event", _noop):
        try:
            verdict = asyncio.run(w._apply_style_lint(JOB["id"], ctx, verdict))
        except Exception as e:
            print("style lint raised (prod fails open?):", e)
    print("== verdict ==", json.dumps(verdict, indent=2))
    if verdict.get("overall") != "fail":
        print("QA gate: COMPLETED")
        return
    classed = classify_qa_failure(verdict, ctx)
    print("classify:", json.dumps(classed, indent=2, default=str))
    if not classed["escalate"]:
        print("PROD WOULD PAUSE: [qa_review]", nonfixable_review_message(classed["nonfixable"]))
    else:
        phases = [p for p in select_escalation_phases(verdict, PHASE_ORDER) if p != "validator"]
        print("PROD WOULD ESCALATE phases:", phases)


if __name__ == "__main__":
    a = sys.argv[1:]

    def opt(name):
        return a[a.index(name) + 1] if name in a else None

    if a[0] == "intake":
        cmd_intake()
    elif a[0] == "prompts":
        cmd_prompts(a[1], opt("--flags"), opt("--model"), opt("--feedback"))
    elif a[0] == "merge":
        cmd_merge(a[1] if len(a) > 1 else "formatter")
    elif a[0] == "gate":
        fn = {
            "formatter": cmd_gate_formatter,
            "seo": cmd_gate_seo,
            "analyst": cmd_gate_analyst,
            "validator": cmd_gate_validator,
        }[a[1]]
        fn(a[2] if len(a) > 2 else None)
