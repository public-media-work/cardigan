"""Manual-supervisor harness: builds the exact prompts prod builds and runs prod's
deterministic gates on phase outputs. Read-only against the repo.

Usage:
  harness.py intake                      -> metrics, chunking decision, backends
  harness.py prompts <phase> [flags.json] [prev.md]
                                         -> writes <phase>/system.md, <phase>/user.md
  harness.py gate formatter [output.md]  -> completeness + seam gates
  harness.py gate validator [output.md]  -> parse + QA-gate routing
"""

import json
import os
import sys
from pathlib import Path

REPO = Path("/Users/mriechers/Developer/cardigan/.claude/worktrees/linear-dreaming-bumblebee")
RUN = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
os.chdir(REPO)

from api.services.chunking import split_transcript  # noqa: E402
from api.services.completeness import check_completeness, count_content_words, count_source_words  # noqa: E402
from api.services.escalation import (
    classify_qa_failure,
    nonfixable_review_message,
    select_escalation_phases,
)  # noqa: E402
from api.services.seam_coverage import DEFAULT_BLOCKING_RATIO, find_dropped_spans, format_gap_message  # noqa: E402
from api.services.utils import calculate_transcript_metrics, get_srt_duration, parse_srt  # noqa: E402
from api.services.worker import JobWorker  # noqa: E402

SRT_PATH = RUN / "6POL0213.srt"
SRT = SRT_PATH.read_text(encoding="utf-8")
JOB = {"id": 9999, "project_name": "6POL0213", "transcript_file": "6POL0213.srt", "media_id": "6POL0213"}
PHASE_ORDER = ["analyst", "formatter", "seo", "validator"]


def load_sst():
    p = RUN / "sst_context.json"
    return json.loads(p.read_text()) if p.exists() else None


ESC = os.environ.get(
    "ESC"
)  # variant subdir name (e.g. esc, final): prefer <phase>/<ESC>/output.md and write prompts there


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
    sst = load_sst()
    content_type = w._detect_content_type(metrics, SRT_PATH, sst)
    JOB["content_type"] = content_type
    ctx = {
        "project_name": JOB["project_name"],
        "transcript": SRT,
        "transcript_file": JOB["transcript_file"],
        "project_path": RUN / "OUTPUT",
        "transcript_metrics": metrics,
        "sst_context": sst,
        "content_type": content_type,
        "diarization_result": None,
    }
    for ph in PHASE_ORDER:
        out = phase_output(ph)
        if out is not None:
            ctx[f"{ph}_output"] = out
    cc = RUN / "formatter" / "completeness.json"
    if cc.exists():
        ctx["completeness_check"] = json.loads(cc.read_text())
    return ctx, metrics


def cmd_intake():
    w = JobWorker()
    ctx, metrics = build_context(w)
    print("== main calculate_transcript_metrics ==")
    print(json.dumps(metrics, indent=2, default=str))
    caps = parse_srt(SRT)
    print("captions:", len(caps), "srt duration min:", round(get_srt_duration(caps) / 60000, 2))
    print("dialogue words (completeness.count_source_words is_srt):", count_source_words(SRT, True))
    print("content_type:", ctx["content_type"])
    print("timestamp phase should run:", w._should_run_timestamp_phase(JOB, metrics, SRT_PATH))
    chunking = w.llm.config["routing"]["chunking"]
    from api.services.speaker_segmentation import split_interior_speaker_changes

    split = split_interior_speaker_changes(SRT)
    print("captions after >> split:", len(parse_srt(split)))
    chunks = split_transcript(split, is_srt=True, config=chunking)
    print("main chunking decision:", "NO CHUNK" if chunks is None else f"{len(chunks)} chunks")
    print("backends:", {p: w.llm.get_backend_for_phase(p) for p in PHASE_ORDER + ["timestamp"]})
    print("phase_models:", w.llm.config.get("phase_models"))
    print("sst_context loaded:", bool(ctx["sst_context"]))


def cmd_prompts(phase, flags_file=None, prev_file=None):
    w = JobWorker()
    ctx, metrics = build_context(w)
    if flags_file:
        ctx["_validation_flags"] = json.loads(Path(flags_file).read_text())
    if prev_file:
        ctx["_previous_output"] = Path(prev_file).read_text()
    # replicate _run_phase pre-steps
    if phase == "formatter":
        seg = w.llm.config.get("routing", {}).get("speaker_segmentation", {})
        if seg.get("enabled", True):
            from api.services.speaker_segmentation import split_interior_speaker_changes

            ctx["transcript"] = split_interior_speaker_changes(ctx["transcript"])
    if phase == "timestamp":
        ctx["srt_content"] = SRT
    ctx.pop("style_pre", None)
    style_cfg = w._style_cfg()
    if style_cfg.get("enabled") and style_cfg.get("phases", {}).get(phase, {}).get("pre"):
        from api.services.style_engine import load_rules, run_pre_stage

        pre = run_pre_stage(phase, ctx, load_rules(style_cfg.get("rules_file", "config/house_style.yaml")))
        if pre.prompt_section:
            ctx["style_pre"] = {"prompt_section": pre.prompt_section, **pre.data}
    if phase == "formatter":
        chunking = w.llm.config["routing"]["chunking"]
        if chunking.get("enabled"):
            chunks = split_transcript(ctx["transcript"], is_srt=True, config=chunking)
            print("chunking decision:", "single call" if chunks is None else f"CHUNKED into {len(chunks)}")
            if chunks is not None:
                write_chunk_prompts(w, ctx, chunks)
    system = w._load_agent_prompt(phase)  # prod never passes model -> placeholder unfilled
    user = w._build_phase_prompt(phase, ctx)
    d = RUN / phase / ESC if ESC else RUN / phase
    d.mkdir(parents=True, exist_ok=True)
    (d / "system.md").write_text(system, encoding="utf-8")
    (d / "user.md").write_text(user, encoding="utf-8")
    print(f"wrote {d}/system.md ({len(system)} chars) and user.md ({len(user)} chars)")
    print("backend:", w.llm.get_backend_for_phase(phase), "default model:", w.llm.config["phase_models"].get(phase))
    print(
        "unfilled placeholders in system prompt:",
        [t for t in ["{model name you are running as}", "{the model you are running as}", "{TODAY"] if t in system],
    )


def write_chunk_prompts(w, context, chunks):
    """Replicates worker._run_formatter_chunked's per-chunk user messages (worker.py:2692-2845)."""
    from api.services.worker import KNOWLEDGE_DIR

    system_prompt = w._load_agent_prompt("formatter")
    analysis = context.get("analyst_output", "")
    sst_context = context.get("sst_context")
    glossary_section = ""
    glossary_path = KNOWLEDGE_DIR / "glossary.md"
    if glossary_path.exists():
        glossary_section = f"\n## Transcript Glossary\n\n{glossary_path.read_text()}\n\n"
    sst_section = ""
    if sst_context:
        sst_section = "\n## Single Source of Truth (SST) Context\n\n"
        for key in [
            "title",
            "program",
            "short_description",
            "long_description",
            "host",
            "presenter",
            "keywords",
            "social_media_description",
            "project_notes",
        ]:
            if sst_context.get(key):
                sst_section += f"**{key.replace('_', ' ').title()}:** {sst_context[key]}\n"
    total_chunks = len(chunks)
    verbatim_instruction = """CRITICAL: You MUST preserve ALL spoken dialogue VERBATIM. Do NOT summarize, condense, paraphrase, or reword.
Every sentence spoken in the transcript must appear in your output using the speaker's actual words.
You may remove filler words (um, uh) and fix grammar/punctuation, but do NOT rephrase, rewrite, or generate new copy.
If the speaker said it, those exact words must appear in the output. Do NOT substitute your own phrasing.
If a caption is garbled or unclear, include your best reconstruction rather than dropping it. NEVER silently omit content.
SPELLING: Always use "partisan" (not "partizan"), "bipartisan" (not "bipartisan"). Program names like "Inside Wisconsin Politics" are NOT italicized."""
    for chunk in chunks:
        section_tail = w._section_tail(chunk.content)
        coverage_mandate = (
            "COVERAGE MANDATE: Format EVERY line of the section below, in order, from its "
            "first line through to its last. Do NOT skip, summarize, or jump over any "
            "portion of the section.\n"
        )
        tail_anchor = (
            "This section ENDS with the following line — your formatted output MUST reach "
            f"and include it (do not stop before it):\n---\n{section_tail}\n---\n"
            if section_tail
            else ""
        )
        if chunk.index == 0:
            user_message = f"{verbatim_instruction}\n\n"
            if total_chunks > 1:
                user_message += coverage_mandate + tail_anchor + "\n"
            if total_chunks > 1:
                user_message += (
                    f"IMPORTANT: This is section 1 of {total_chunks} of a long transcript "
                    "being processed in parts. Later sections cover the rest of the "
                    "transcript. Your section legitimately ends partway through — do NOT "
                    "assess overall transcript completeness, do NOT claim the transcript "
                    "is truncated, incomplete, or cut off, and do NOT set a 'needs_review' "
                    "status on that basis. Format only the dialogue in this section.\n\n"
                )
            user_message += "Using the following analysis as guidance:\n\n"
            if sst_section:
                user_message += sst_section
            if glossary_section:
                user_message += glossary_section
            user_message += f"---\n{analysis}\n---\n\n"
            user_message += f"Please format this transcript:\n\n---\n{chunk.content}\n---"
        else:
            user_message = f"""{verbatim_instruction}
{glossary_section}
IMPORTANT: This is section {chunk.index + 1} of {total_chunks} of a long transcript being processed in parts.
DO NOT generate the metadata header (Project, Program, Duration, Date).
DO NOT generate "# Formatted Transcript" heading.
Begin directly with speaker attribution and dialogue.
The previous section ended with:
---
{chunk.overlap_prefix}
---
Continue formatting from where the previous section left off. Do NOT repeat content from the overlap above.
CRITICAL: Pay careful attention to which speaker is talking. Use the overlap above to identify who was speaking last and maintain correct attribution. Getting the wrong name on a statement is worse than using a generic label.

Using the following analysis as guidance:
---
{analysis}
---

{coverage_mandate}{tail_anchor}
Please format this transcript section:

---
{chunk.content}
---"""
        style_pre = context.get("style_pre") or {}
        style_section = style_pre.get("prompt_section")
        if style_section:
            user_message = f"{user_message}\n\n{style_section}"
        d = RUN / "formatter" / f"chunk{chunk.index}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "system.md").write_text(system_prompt, encoding="utf-8")
        (d / "user.md").write_text(user_message, encoding="utf-8")
        (d / "chunk_meta.json").write_text(
            json.dumps(
                {
                    "index": chunk.index,
                    "word_count": chunk.word_count,
                    "caption_range": getattr(chunk, "caption_range", None),
                    "start": getattr(chunk, "start_time", None),
                    "end": getattr(chunk, "end_time", None),
                    "tail": section_tail,
                },
                default=str,
            )
        )
        print(f"  chunk {chunk.index}: {chunk.word_count} words, user.md {len(user_message)} chars -> {d}")


def cmd_merge(base="formatter"):
    from api.services.chunking import merge_formatter_chunks

    parts = []
    i = 0
    while (RUN / base / f"chunk{i}" / "output.md").exists():
        parts.append((RUN / base / f"chunk{i}" / "output.md").read_text(encoding="utf-8"))
        i += 1
    merged = merge_formatter_chunks(parts)
    (RUN / base / "output.md").write_text(merged, encoding="utf-8")
    print(f"merged {i} chunks -> {base}/output.md ({len(merged)} chars)")
    head = merged.split("\n---", 1)[0]
    print("=== merged header ===\n" + head[:800])


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
    (RUN / "formatter").mkdir(exist_ok=True)
    (RUN / "formatter" / "completeness.json").write_text(json.dumps(res.to_dict()))
    if not res.is_complete and not res.skipped:
        print(
            f"PROD WOULD PAUSE: [truncation] TRUNCATION DETECTED: Formatter output covers only "
            f"{res.coverage_ratio:.0%} of source transcript ({res.output_word_count:,} / "
            f"{res.source_word_count:,} words). Retry to escalate to a more capable model."
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
            for m in ["<!-- review notes", "status:** needs_review", "status:** ready_for_editing", "section 1 of"]
        },
    )
    print("content words:", count_content_words(out))


def cmd_gate_validator(out_file=None):
    w = JobWorker()
    ctx, _ = build_context(w)
    out = Path(out_file).read_text() if out_file else phase_output("validator")
    try:
        verdict = JobWorker._parse_validation_result(out)
    except Exception as e:
        print("JSON PARSE FAILED:", e, "-> prod stores validation_data=None -> job COMPLETES with no verdict (gap)")
        return
    print("== verdict ==", json.dumps(verdict, indent=2))
    overall = verdict.get("overall")
    if overall != "fail":
        print("QA gate: COMPLETED")
        return
    classed = classify_qa_failure(verdict, ctx)
    print("classify:", json.dumps(classed, indent=2))
    if not classed["escalate"]:
        print("PROD WOULD PAUSE: [qa_review]", nonfixable_review_message(classed["nonfixable"]))
    else:
        phases = [p for p in select_escalation_phases(verdict, PHASE_ORDER) if p != "validator"]
        print("PROD WOULD ESCALATE phases:", phases, "(haiku->sonnet, sonnet->opus via model_roster.newest_in_family)")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[0] == "intake":
        cmd_intake()
    elif a[0] == "prompts":
        cmd_prompts(a[1], a[2] if len(a) > 2 else None, a[3] if len(a) > 3 else None)
    elif a[0] == "merge":
        cmd_merge(a[1] if len(a) > 1 else "formatter")
    elif a[0] == "gate" and a[1] == "formatter":
        cmd_gate_formatter(a[2] if len(a) > 2 else None)
    elif a[0] == "gate" and a[1] == "validator":
        cmd_gate_validator(a[2] if len(a) > 2 else None)
