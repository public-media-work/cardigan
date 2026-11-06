"""Slice builders for the context bundle (#427).

Deterministic: ``sst``, ``job``, ``captions``, ``house_rules``. Inferred:
``outline``. (``speakers`` and ``keyword_seed`` follow in their own PRs.)

AirTable is optional. With no record — or AirTable unreachable — the ``sst``
slice is still *ready*, with ``fetched: false``: generators run without a
basis, as a first author, instead of becoming unavailable.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from api.models.item import GenerationState, Origin
from api.services.generation.contract import (
    BundleView,
    Caps,
    GenerationFailure,
    SliceResult,
    SliceSpec,
    UnitContext,
    parse_json_object,
)
from api.services.items import PREFIX_PROGRAMS, SST_FIELD_IDS, TITLE_SEPARATOR, spelling_conflicts, unsupported_terms

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts" / "generation"

# SST fields producers fill during production (#426 fill-rate read): the best
# available spelling of people, places and lower-thirds. Read, never pushed.
SST_PRODUCER_FIELD_IDS: dict[str, str] = {
    "fldO6YFVecVjLtMTy": "Lower Thirds",
    "fldRDOZbuT0cmckNZ": "Episodic Lower Thirds",
    "fld6qUVBiaVF1dsr0": "Main Location City",
    "fldhiTJdqrANLdqSA": "Main Location County",
    "fldePuqz9XgF587Pc": "Secondary Location",
    "fldLkYelFOIg0DyXC": "Secondary Location County",
    "fldS3oCS9K5IyAKxA": "Taping Location",
    "fldyNLTitzM2qV1wQ": "Batch-Episode",
}
SST_PROJECT_FIELD_ID = "fld3su0x59DeTog76"  # Project (link) — program identity lives on it
PROJECT_NAME_FIELD = "Project Name"

# Under this many minutes a job is a Short (matches the V4 worker's threshold).
SHORT_MAX_MINUTES = 1.5
# Outline prompt: captions grouped into blocks of about this many seconds.
OUTLINE_BLOCK_SECONDS = 30
DEFAULT_MODEL = "anthropic/claude-sonnet-4.6"


def _prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# sst
# ---------------------------------------------------------------------------


async def fetch_sst_record(job: Any, *, pin: bool = True) -> tuple[Optional[dict], Optional[str], Optional[str]]:
    """Resolve, pin and fetch the job's SST record.

    Returns ``(record, program, reason)``. The record is fetched by field id
    (the base renames fields in place). A job already pinned to a record uses
    it; otherwise the Media ID is looked up and the result is pinned, so a
    Media ID edited mid-review can't redirect the job to another record.
    """
    if not job.airtable_record_id and not job.media_id:
        return None, None, "no_media_id"

    from api.models.job import JobUpdate
    from api.services.airtable import get_airtable_client
    from api.services.database import update_job

    try:
        client = get_airtable_client()
        record_id = job.airtable_record_id
        if not record_id:
            found = await client.search_sst_by_media_id(job.media_id)
            if found is None:
                return None, None, "no_sst_record"
            record_id = found["id"]
            if pin:
                await update_job(job.id, JobUpdate(airtable_record_id=record_id))

        record = await client.get_sst_record(record_id, by_field_id=True)
        if record is None:
            return None, None, "no_sst_record"

        program = None
        project_ids = (record.get("fields") or {}).get(SST_PROJECT_FIELD_ID)
        if isinstance(project_ids, list) and project_ids:
            try:
                project = await client.get_project_record(project_ids[0])
                program = ((project or {}).get("fields") or {}).get(PROJECT_NAME_FIELD)
            except Exception as err:  # the program is context, not a requirement
                logger.debug("Could not read the linked Project", extra={"error": str(err)})
        return record, program, None

    except Exception as err:
        logger.warning(
            "Could not fetch the SST record for the bundle",
            extra={"job_id": job.id, "media_id": job.media_id, "error": str(err)},
        )
        return None, None, "airtable_unreachable"


async def build_sst(view: BundleView, ctx: UnitContext) -> SliceResult:
    record, project, reason = await fetch_sst_record(ctx.job, pin=ctx.inputs.get("pin_sst", True))
    if record is None:
        return SliceResult(
            content={"fetched": False, "record_id": None, "program": None, "project": None, "fields": {}},
            reason=reason,
        )

    raw = record.get("fields") or {}
    wanted = list(dict.fromkeys(list(SST_FIELD_IDS) + list(SST_PRODUCER_FIELD_IDS) + [SST_PROJECT_FIELD_ID]))
    return SliceResult(
        content={
            "fetched": True,
            "record_id": record.get("id"),
            "program": series_name(project),
            "project": project,
            "fields": {field_id: raw.get(field_id) for field_id in wanted},
        }
    )


def series_name(project: Optional[str]) -> Optional[str]:
    """The program a title names, from the linked Project's name.

    Project names carry a season ("Wisconsin Life | Season 13"); the title
    convention and ``programs:`` in house_style key on the series alone.
    """
    if not project or not project.strip():
        return None
    return project.split(TITLE_SEPARATOR)[0].strip()


def known_text(*slices: Optional[dict]) -> str:
    """Every source string in the given slices, for the grounding check."""
    chunks: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            chunks.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for content in slices:
        if not content:
            continue
        # Cue lists repeat the caption text; walking them would double the work.
        walk({k: v for k, v in content.items() if k != "cues"})
    return "\n".join(chunks)


def sst_context_for_items(sst: Optional[dict]) -> Optional[dict]:
    """The ``{field_id: value, record_id}`` shape ``build_items`` reads, or None."""
    if not sst or not sst.get("fetched"):
        return None
    return {**(sst.get("fields") or {}), "record_id": sst.get("record_id")}


# ---------------------------------------------------------------------------
# captions
# ---------------------------------------------------------------------------


async def build_captions(view: BundleView, ctx: UnitContext) -> SliceResult:
    """Parse the job's caption file into timed cues.

    SRT is parsed; plain text takes the degraded path (``timed: false``) and
    anything that needs timings is unavailable downstream. SCC waits on #434.
    """
    from api.services.utils import get_srt_duration, parse_srt

    srt_text: Optional[str] = ctx.inputs.get("srt_text")
    transcript_text: Optional[str] = ctx.inputs.get("transcript_text")
    if srt_text is None and transcript_text is None:
        raise GenerationFailure("missing_input", "no caption file was supplied")

    cues = parse_srt(srt_text) if srt_text else []
    if cues:
        text = " ".join(cue.text.replace("\n", " ").strip() for cue in cues)
        duration_ms = get_srt_duration(cues)
        gaps = sum(1 for prev, cur in zip(cues, cues[1:]) if cur.start_ms - prev.end_ms > 2000)
        content = {
            "timed": True,
            "cues": [{"start_ms": c.start_ms, "end_ms": c.end_ms, "text": c.text} for c in cues],
            "text": text,
            "stats": {
                "cue_count": len(cues),
                "duration_ms": duration_ms,
                "word_count": len(text.split()),
                "gaps_over_2s": gaps,
                "inaudible": text.lower().count("[inaudible"),
            },
        }
    else:
        text = (transcript_text or srt_text or "").strip()
        content = {
            "timed": False,
            "cues": [],
            "text": text,
            "stats": {
                "cue_count": 0,
                "duration_ms": None,
                "word_count": len(text.split()),
                "gaps_over_2s": None,
                "inaudible": text.lower().count("[inaudible"),
            },
        }
    return SliceResult(content=content)


# ---------------------------------------------------------------------------
# job
# ---------------------------------------------------------------------------


async def build_job(view: BundleView, ctx: UnitContext) -> SliceResult:
    sst = view.get("sst") or {}
    captions = view.get("captions") or {}
    stats = captions.get("stats") or {}
    job = ctx.job

    duration_ms = stats.get("duration_ms")
    duration_minutes = round(duration_ms / 60000, 2) if duration_ms else job.duration_minutes
    content_type = job.content_type or (
        "short" if duration_minutes and 0 < duration_minutes < SHORT_MAX_MINUTES else "full"
    )
    program = sst.get("program")

    return SliceResult(
        content={
            "job_id": job.id,
            "media_id": job.media_id,
            "program": program,
            "content_type": content_type,
            "duration_minutes": duration_minutes,
            "word_count": stats.get("word_count"),
            "title_convention": {
                "separator": TITLE_SEPARATOR,
                "program_position": "prefix" if program in PREFIX_PROGRAMS else "suffix",
            },
            "copy_generation": ctx.copy_mode.value,
            "settings": ctx.inputs.get("resolved_settings") or {},
        }
    )


# ---------------------------------------------------------------------------
# house_rules
# ---------------------------------------------------------------------------


async def build_house_rules(view: BundleView, ctx: UnitContext) -> SliceResult:
    from api.services.glossary import get_glossary_path
    from api.services.style_engine.prompt_blocks import render_prompt_blocks
    from api.services.style_engine.rules import load_rules

    job = view.get("job") or {}
    program = job.get("program")
    rules = load_rules()

    glossary_path = get_glossary_path()
    glossary = glossary_path.read_text(encoding="utf-8") if glossary_path.is_file() else None

    return SliceResult(
        content={
            "copy_rules": render_prompt_blocks("{{style:seo.copy_rules}}", rules=rules).strip(),
            "limits": rules.limits_for(program, content_type=job.get("content_type") or "full"),
            "program_rules": rules.program_rules(program),
            "glossary": glossary,
        }
    )


# ---------------------------------------------------------------------------
# outline (inferred)
# ---------------------------------------------------------------------------


def _timecode(ms: int) -> str:
    seconds = ms // 1000
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def captions_as_blocks(captions: dict, block_seconds: int = OUTLINE_BLOCK_SECONDS) -> str:
    """Captions as '[M:SS] text' blocks — timings a model can cite, at a
    fraction of the tokens of cue-by-cue SRT."""
    cues = captions.get("cues") or []
    if not cues:
        return captions.get("text") or ""
    lines, block, block_start = [], [], None
    for cue in cues:
        if block_start is None:
            block_start = cue["start_ms"]
        block.append(cue["text"].replace("\n", " ").strip())
        if cue["end_ms"] - block_start >= block_seconds * 1000:
            lines.append(f"[{_timecode(block_start)}] {' '.join(block)}")
            block, block_start = [], None
    if block:
        lines.append(f"[{_timecode(block_start)}] {' '.join(block)}")
    return "\n".join(lines)


async def build_outline(view: BundleView, ctx: UnitContext) -> SliceResult:
    job = view.get("job") or {}
    captions = view.get("captions") or {}
    sst = view.get("sst") or {}
    if not (captions.get("text") or "").strip():
        raise GenerationFailure("missing_input", "the captions are empty")

    fields = sst.get("fields") or {}
    known = {name: fields.get(fid) for fid, name in SST_PRODUCER_FIELD_IDS.items() if fields.get(fid)}
    user = (
        f"## Job\n\nProgram: {job.get('program') or '(unknown)'}\n"
        f"Duration: {job.get('duration_minutes') or '(unknown)'} minutes\n"
        f"Timed: {'yes' if captions.get('timed') else 'no'}\n"
    )
    if known:
        user += "\n## Producer-entered SST fields (authoritative spellings)\n\n"
        user += "\n".join(f"- {name}: {value}" for name, value in known.items()) + "\n"
    user += f"\n## Captions\n\n{captions_as_blocks(captions)}\n"

    call = await ctx.complete(_prompt("outline.md"), user)
    data = parse_json_object(call.content)

    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise GenerationFailure("malformed_output", "the outline has no summary")
    themes = [t for t in data.get("themes") or [] if isinstance(t, str) and t.strip()]
    segments = [
        {"start": s.get("start"), "summary": s.get("summary")}
        for s in data.get("segments") or []
        if isinstance(s, dict) and isinstance(s.get("summary"), str)
    ]
    outline_text = "\n".join([summary, *themes, *(s["summary"] for s in segments)])
    sources = known_text(captions, sst)
    return SliceResult(
        content={
            "summary": summary.strip(),
            "themes": themes,
            "segments": segments,
            # Names the outline uses that the sources never contain: candidates
            # for a correction here, before every generator copies them.
            "unverified_terms": unsupported_terms(outline_text, sources),
            # Names spelled one way here and mostly another way in the sources.
            "name_conflicts": [
                {"used": used, "preferred": preferred, "used_count": used_n, "preferred_count": preferred_n}
                for used, preferred, used_n, preferred_n in spelling_conflicts(outline_text, sources)
            ],
        }
    )


SLICE_SPECS: tuple[SliceSpec, ...] = (
    SliceSpec("sst", Origin.deterministic, build_sst),
    SliceSpec("captions", Origin.deterministic, build_captions, rebuildable=False),
    SliceSpec("job", Origin.deterministic, build_job, reads=("sst", "captions")),
    SliceSpec("house_rules", Origin.deterministic, build_house_rules, reads=("job",)),
    SliceSpec(
        "outline",
        Origin.inferred,
        build_outline,
        reads=("job", "captions", "sst"),
        model=DEFAULT_MODEL,
        caps=Caps(max_cost_usd=0.25, max_output_tokens=4000),
    ),
)

__all__ = ["SLICE_SPECS", "GenerationState", "fetch_sst_record", "sst_context_for_items", "captions_as_blocks"]
