#!/usr/bin/env python3
"""Task 2c -- offline lint agreement study over historical production jobs.

Runs the deterministic validator checklist (``api.services.style_engine.lint.
run_lint``) over HISTORICAL production job artifacts pulled read-only from
the Cardigan production API, and diffs the result against the LLM
validator's stored verdict (``Job.validation_result``) for the same jobs.
Zero LLM calls -- this is the trust-building instrument for turning the lint
suite on: it answers "does lint catch at least what the LLM validator
already catches, for the categories lint is designed to cover?" without
spending a single token.

Two halves, deliberately separated:

- Pure comparison logic (``classify_flag``, ``compare_phase``,
  ``build_job_matrix``, ``aggregate_matrices``, ``select_eligible_jobs``,
  ``build_context``) -- no network, no filesystem beyond what callers pass
  in. Covered by tests/test_lint_agreement_study.py with synthetic data.
- Network/fetch/orchestration (``fetch_queue_cached``, ``fetch_job_bundle``,
  ``main``) -- httpx GETs against the production API, cached to disk. Not
  unit-tested; exercised by actually running the study.

Usage:
    python -m scripts.lint_agreement_study [--base-url https://cardigan.riechers.co]
        [--jobs 1,2,...|all] [--cache-dir OUTPUT/eval/prod_artifacts]
        [--refresh] [--out planning/hybrid-pipeline-eval/stage2-lint-agreement]

READ-ONLY against production: GETs only, 0.2s sleep between requests. Never
POST/PATCH/DELETE. If the production API is unreachable, this exits 2 and
prints a BLOCKED message -- it never fabricates results.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from api.services.style_engine.lint import run_lint
from api.services.style_engine.rules import StyleRulesError, load_rules
from api.services.style_engine.types import PhaseCheckResult, RuleViolation

DEFAULT_BASE_URL = "https://cardigan.riechers.co"
DEFAULT_CACHE_DIR = Path("OUTPUT/eval/prod_artifacts")
DEFAULT_OUT_DIR = Path("planning/hybrid-pipeline-eval/stage2-lint-agreement")
DEFAULT_RULES_PATH = Path("config/house_style.yaml")

REQUEST_SLEEP_SECONDS = 0.2

# Matches lint.py's _CANONICAL_PHASES / qa_merge.py's _SKELETON_PHASES --
# the three phases both run_lint and the LLM validator's phase_results cover.
CANONICAL_PHASES = ("analyst", "formatter", "seo")

OUTPUT_FILENAMES: dict[str, str] = {
    "analyst": "analyst_output.md",
    "formatter": "formatter_output.md",
    "seo": "seo_output.md",
}

# ---------------------------------------------------------------------------
# Deterministic-category keyword map
#
# Classifies each LLM validator flag string as belonging to zero or more
# "deterministic" categories -- the mechanical, format-level checks that
# prompts/validator.md's checklist describes and that lint.run_lint
# re-implements as code (char limits/lengths, missing/empty output, review
# notes, placeholder text, speaker label FORMAT consistency, content past
# duration, truncation). Anything matching none of these is SEMANTIC --
# content-accuracy/relevance/quality judgment calls that require
# understanding the transcript, out of lint's scope by design.
#
# This is intentionally a plain-substring keyword map (not an NLP
# classifier) -- easy to audit, easy to extend, and its false
# positives/negatives are caught by the per-item human analysis the study
# report does over every llm_only_deterministic flag (see study.md).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeterministicCategory:
    """One deterministic-category keyword rule.

    ``keywords``: any (case-insensitive) substring match is sufficient to
    put a flag in this category. ``exclude_keywords``: if ANY of these also
    appears, the category does NOT apply -- used to keep content-judgment
    flags (misattribution, ambiguity, unresolved identity) that happen to
    mention "speaker label" out of the deterministic bucket, since lint has
    no way to judge whether a *specific* line of dialogue was attributed to
    the right person; it can only judge label *shape* (single word,
    honorific, two labels that look like the same person spelled two ways).
    ``rule_ids``: the ``lint.*`` rule_id family this category corresponds
    to, for matrix correspondence. Empty for the special-cased char-limit
    category, whose rule_id depends on which field the flag names (see
    ``_char_limit_rule_id``).
    """

    name: str
    keywords: tuple[str, ...]
    exclude_keywords: tuple[str, ...] = ()
    rule_ids: tuple[str, ...] = ()


DETERMINISTIC_CATEGORIES: tuple[DeterministicCategory, ...] = (
    DeterministicCategory(
        name="output_missing",
        keywords=(
            "output missing",
            "output is missing",
            "missing or empty",
            "empty output",
            "missing or has fewer than",
        ),
        rule_ids=("lint.output_missing",),
    ),
    DeterministicCategory(
        name="placeholder_text",
        keywords=(
            "placeholder text",
            "template artifact",
            "{media_id}",
            "[insert",
            "{today",
            "{model name",
            "unfilled placeholder",
        ),
        rule_ids=("lint.placeholder_text",),
    ),
    DeterministicCategory(
        name="review_notes",
        keywords=(
            "review note",
            "review notes",
            "html comment",
            "editorial instructions",
            "appear in transcript body",
            "appear in the transcript body",
            "embedded in transcript body",
            "embedded in the transcript body",
            "editorial metadata must not appear",
            "agent instructions",
        ),
        rule_ids=("lint.formatter.review_notes_in_body",),
    ),
    DeterministicCategory(
        name="speaker_label_format",
        keywords=(
            "single-word",
            "single word label",
            "labeled inconsistently",
            "generic label",
            "generic 'speaker",
            'generic "speaker',
            "honorific",
            "speaker label",
        ),
        # Content-judgment terms that mean the flag is really about WHO said
        # a line (semantic), not about label SHAPE (deterministic) -- see
        # DeterministicCategory docstring above.
        exclude_keywords=(
            "misattribut",
            "attribut",
            "unclear",
            "ambigu",
            "unverified",
            "unconfirmed",
            "unresolved",
            "inverted",
        ),
        rule_ids=("lint.formatter.speaker_label_inconsistent",),
    ),
    DeterministicCategory(
        name="content_past_duration",
        keywords=(
            "past the episode",
            "past the content duration",
            "exceeds the content duration",
            "beyond the episode duration",
            "content past duration",
            "after the episode ends",
            "past the video duration",
        ),
        rule_ids=("lint.formatter.content_past_duration",),
    ),
    DeterministicCategory(
        name="truncation",
        keywords=(
            "truncat",
            "ends abruptly",
            "mid-sentence",
            "cut off",
            "cuts off",
            "abrupt end",
            "ends mid-sentence",
            "cutoff",
            "missing from the formatted",
            "content is missing from",
        ),
        rule_ids=("lint.formatter.truncation_suspect",),
    ),
    DeterministicCategory(
        name="keyword_count",
        keywords=(
            "keyword count",
            "keywords recommended",
            "tags recommended",
            "expected 15-20",
            "expected 5-10",
            "too few keywords",
            "too many keywords",
        ),
        rule_ids=("lint.seo.keywords_count",),
    ),
)

# Special-cased: "char_limit" needs BOTH a "characters" mention AND a
# violation word (exceed/over/too long), plus a *field* name to know which
# lint.seo.*_over_limit rule_id it corresponds to -- see _char_limit_rule_id.
_CHAR_LIMIT_HAS_CHAR_RE = re.compile(r"\bchar(?:s|acters?)?\b", re.IGNORECASE)
_CHAR_LIMIT_HAS_VIOLATION_RE = re.compile(r"\bexceed\w*\b|\bover\b|\btoo long\b|\bover-limit\b", re.IGNORECASE)


def _char_limit_rule_id(lower_text: str) -> str | None:
    """Which lint.seo.*_over_limit rule_id (if any) a char-limit flag names.

    Checked as multi-word substrings first ("short description" / "long
    description") so a flag mentioning both a description field and the
    word "title" elsewhere doesn't get misrouted to the title rule.
    """
    if "short description" in lower_text:
        return "lint.seo.short_over_limit"
    if "long description" in lower_text:
        return "lint.seo.long_over_limit"
    if "title" in lower_text:
        return "lint.seo.title_over_limit"
    return None


@dataclass(frozen=True)
class FlagClassification:
    """The classification result for one LLM validator flag string."""

    text: str
    categories: tuple[str, ...]
    rule_ids: tuple[str, ...]

    @property
    def is_deterministic(self) -> bool:
        return bool(self.categories)

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "categories": list(self.categories), "rule_ids": list(self.rule_ids)}


def classify_flag(text: str) -> FlagClassification:
    """Classify one LLM validator flag string as deterministic-category(ies) or semantic.

    Pure function -- no I/O, no state beyond the module-level keyword map.
    """
    lower = text.lower()
    categories: list[str] = []
    rule_ids: set[str] = set()

    for cat in DETERMINISTIC_CATEGORIES:
        if not any(kw in lower for kw in cat.keywords):
            continue
        if cat.exclude_keywords and any(kw in lower for kw in cat.exclude_keywords):
            continue
        categories.append(cat.name)
        rule_ids.update(cat.rule_ids)

    if _CHAR_LIMIT_HAS_CHAR_RE.search(text) and _CHAR_LIMIT_HAS_VIOLATION_RE.search(text):
        categories.append("char_limit")
        field_rule_id = _char_limit_rule_id(lower)
        if field_rule_id:
            rule_ids.add(field_rule_id)

    return FlagClassification(text=text, categories=tuple(categories), rule_ids=tuple(sorted(rule_ids)))


# ---------------------------------------------------------------------------
# Matrix building -- pure, operates on already-computed PhaseCheckResults
# and already-fetched validation_result dicts. No network.
# ---------------------------------------------------------------------------


@dataclass
class PhaseComparison:
    phase: str
    lint_violation_count: int
    llm_flags_deterministic: list[dict[str, Any]] = field(default_factory=list)
    llm_flags_semantic: list[str] = field(default_factory=list)
    both_caught: list[dict[str, Any]] = field(default_factory=list)
    lint_only: list[dict[str, Any]] = field(default_factory=list)
    llm_only_deterministic: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "lint_violation_count": self.lint_violation_count,
            "llm_flags_deterministic": self.llm_flags_deterministic,
            "llm_flags_semantic": self.llm_flags_semantic,
            "both_caught": self.both_caught,
            "lint_only": self.lint_only,
            "llm_only_deterministic": self.llm_only_deterministic,
        }


def compare_phase(phase: str, lint_result: PhaseCheckResult, llm_flags: list[str]) -> PhaseComparison:
    """Diff one phase's lint violations against one phase's LLM flags.

    Correspondence is by rule_id-family membership (a classified flag's
    ``rule_ids`` set), not exact text matching. Each deterministic LLM flag
    claims at most one still-unclaimed lint violation whose rule_id is in
    its rule_ids set (first match, order of ``lint_result.violations``) --
    this preserves the information that lint found MORE instances of a
    rule_id family than the LLM flagged (those extras land in lint_only
    rather than being silently absorbed), while still crediting agreement
    at the rule_id-family granularity the brief specifies.
    """
    classified = [classify_flag(f) for f in llm_flags]
    deterministic = [c for c in classified if c.is_deterministic]
    semantic = [c.text for c in classified if not c.is_deterministic]

    pool: list[RuleViolation] = list(lint_result.violations)
    both_caught: list[dict[str, Any]] = []
    llm_only_deterministic: list[dict[str, Any]] = []

    for c in deterministic:
        match_idx = next((i for i, v in enumerate(pool) if v.rule_id in c.rule_ids), None)
        if match_idx is not None:
            violation = pool.pop(match_idx)
            both_caught.append(
                {
                    "rule_id": violation.rule_id,
                    "lint_message": violation.message,
                    "llm_text": c.text,
                    "categories": list(c.categories),
                }
            )
        else:
            llm_only_deterministic.append(
                {"text": c.text, "categories": list(c.categories), "rule_ids": list(c.rule_ids)}
            )

    lint_only = [v.to_dict() for v in pool]

    return PhaseComparison(
        phase=phase,
        lint_violation_count=len(lint_result.violations),
        llm_flags_deterministic=[c.to_dict() for c in deterministic],
        llm_flags_semantic=semantic,
        both_caught=both_caught,
        lint_only=lint_only,
        llm_only_deterministic=llm_only_deterministic,
    )


@dataclass
class JobMatrix:
    job_id: int
    status: str | None
    content_type: str | None
    duration_minutes: float | None
    validation_result_present: bool
    phases: dict[str, PhaseComparison]

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "content_type": self.content_type,
            "duration_minutes": self.duration_minutes,
            "validation_result_present": self.validation_result_present,
            "phases": {phase: comp.to_dict() for phase, comp in self.phases.items()},
        }


def build_job_matrix(
    job_id: int,
    status: str | None,
    content_type: str | None,
    duration_minutes: float | None,
    lint_results: dict[str, PhaseCheckResult],
    validation_result: dict | None,
) -> JobMatrix:
    """Build the per-job, per-phase comparison matrix. Pure -- no I/O."""
    phase_results = (validation_result or {}).get("phase_results") or {}

    phases: dict[str, PhaseComparison] = {}
    for phase in CANONICAL_PHASES:
        flags = list((phase_results.get(phase) or {}).get("flags") or [])
        lint_result = lint_results.get(phase) or PhaseCheckResult(phase=phase)
        phases[phase] = compare_phase(phase, lint_result, flags)

    return JobMatrix(
        job_id=job_id,
        status=status,
        content_type=content_type,
        duration_minutes=duration_minutes,
        validation_result_present=validation_result is not None,
        phases=phases,
    )


def aggregate_matrices(job_matrices: list[JobMatrix]) -> dict[str, Any]:
    """Sum per-job matrices into aggregate totals, by-phase, and by-category tallies."""
    totals = {"both_caught": 0, "lint_only": 0, "llm_only_deterministic": 0, "llm_semantic": 0}
    by_phase: dict[str, dict[str, int]] = {
        phase: {"both_caught": 0, "lint_only": 0, "llm_only_deterministic": 0, "llm_semantic": 0}
        for phase in CANONICAL_PHASES
    }
    by_category: dict[str, dict[str, int]] = {}

    def _bump_category(cat: str, key: str) -> None:
        by_category.setdefault(cat, {"both_caught": 0, "llm_only_deterministic": 0})
        by_category[cat][key] += 1

    for jm in job_matrices:
        for phase, comp in jm.phases.items():
            totals["both_caught"] += len(comp.both_caught)
            totals["lint_only"] += len(comp.lint_only)
            totals["llm_only_deterministic"] += len(comp.llm_only_deterministic)
            totals["llm_semantic"] += len(comp.llm_flags_semantic)

            by_phase[phase]["both_caught"] += len(comp.both_caught)
            by_phase[phase]["lint_only"] += len(comp.lint_only)
            by_phase[phase]["llm_only_deterministic"] += len(comp.llm_only_deterministic)
            by_phase[phase]["llm_semantic"] += len(comp.llm_flags_semantic)

            for item in comp.both_caught:
                for cat in item["categories"]:
                    _bump_category(cat, "both_caught")
            for item in comp.llm_only_deterministic:
                for cat in item["categories"]:
                    _bump_category(cat, "llm_only_deterministic")

    return {"totals": totals, "by_phase": by_phase, "by_category": by_category}


def select_eligible_jobs(jobs: list[dict]) -> list[int]:
    """Which job ids to study: completed jobs, plus any other-status job that
    still has all three phase outputs on its manifest (paused-after-seo jobs
    etc.) -- "skip paused/failed unless outputs exist" from the brief. Pure.
    """
    eligible: list[int] = []
    for job in jobs:
        status = job.get("status")
        outputs = job.get("outputs") or {}
        has_all_outputs = (
            bool(outputs.get("analysis"))
            and bool(outputs.get("formatted_transcript"))
            and bool(outputs.get("seo_metadata"))
        )
        if status == "completed" or has_all_outputs:
            eligible.append(job["id"])
    return sorted(eligible)


def build_context(job: dict, outputs: dict[str, str | None]) -> dict[str, Any]:
    """Build the run_lint context dict for one job from its record + fetched outputs.

    Mirrors the worker's context bus keys (see lint.py's run_lint docstring).
    ``transcript``/full transcript text is never available here (read-only
    REST, no transcript-fetch endpoint) -- included as None for documentation
    of what's absent; no current lint check reads it (grep-verified against
    api/services/style_engine/lint.py: only duration_minutes, content_type,
    and program are read from context besides the three *_output keys).
    """
    return {
        "analyst_output": outputs.get("analyst"),
        "formatter_output": outputs.get("formatter"),
        "seo_output": outputs.get("seo"),
        "duration_minutes": job.get("duration_minutes"),
        "content_type": job.get("content_type"),
        "transcript_file": job.get("transcript_file"),
        "transcript": None,
        "program": None,
    }


# ---------------------------------------------------------------------------
# Network / fetch / caching -- NOT unit-tested. GETs only, 0.2s sleep between
# requests, cached to --cache-dir, --refresh forces re-download.
# ---------------------------------------------------------------------------


def fetch_queue_cached(client: Any, base_url: str, cache_dir: Path, refresh: bool) -> list[dict]:
    cache_file = cache_dir / "_queue.json"
    if cache_file.exists() and not refresh:
        return json.loads(cache_file.read_text())["jobs"]

    resp = client.get(f"{base_url}/api/queue/", params={"page_size": 100})
    resp.raise_for_status()
    data = resp.json()
    time.sleep(REQUEST_SLEEP_SECONDS)

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(data, indent=2))
    return data["jobs"]


def fetch_job_bundle(
    client: Any, base_url: str, job_id: int, cache_dir: Path, refresh: bool
) -> tuple[dict, dict[str, str | None]]:
    """Fetch (and cache) one job's record + its three phase outputs.

    Cache layout: ``<cache_dir>/job{id}/job.json`` and
    ``<cache_dir>/job{id}/{phase}_output.md``. A ``{filename}.missing``
    marker records a non-200 output fetch so re-runs without --refresh don't
    re-request files already known to be absent.
    """
    job_dir = cache_dir / f"job{job_id}"
    job_dir.mkdir(parents=True, exist_ok=True)

    job_file = job_dir / "job.json"
    if job_file.exists() and not refresh:
        job = json.loads(job_file.read_text())
    else:
        resp = client.get(f"{base_url}/api/jobs/{job_id}")
        resp.raise_for_status()
        job = resp.json()
        time.sleep(REQUEST_SLEEP_SECONDS)
        job_file.write_text(json.dumps(job, indent=2))

    outputs: dict[str, str | None] = {}
    for phase, filename in OUTPUT_FILENAMES.items():
        out_file = job_dir / filename
        missing_marker = job_dir / f"{filename}.missing"

        if out_file.exists() and not refresh:
            outputs[phase] = out_file.read_text()
            continue
        if missing_marker.exists() and not refresh:
            outputs[phase] = None
            continue

        resp = client.get(f"{base_url}/api/jobs/{job_id}/outputs/{filename}")
        time.sleep(REQUEST_SLEEP_SECONDS)
        if resp.status_code >= 400:
            missing_marker.write_text("")
            outputs[phase] = None
        else:
            missing_marker.unlink(missing_ok=True)
            out_file.write_text(resp.text)
            outputs[phase] = resp.text

    return job, outputs


# ---------------------------------------------------------------------------
# Report emission
# ---------------------------------------------------------------------------


def write_agreement_json(
    path: Path, meta: dict[str, Any], job_matrices: list[JobMatrix], aggregate: dict[str, Any]
) -> None:
    payload = {
        "meta": meta,
        "aggregate": aggregate,
        "jobs": [jm.to_dict() for jm in job_matrices],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")


def _category_keyword_map_markdown() -> str:
    lines = [
        "| Category | Keywords (substring, case-insensitive) | Excludes | Corresponding lint rule_id |",
        "|---|---|---|---|",
    ]
    for cat in DETERMINISTIC_CATEGORIES:
        kw = ", ".join(f"`{k}`" for k in cat.keywords)
        ex = ", ".join(f"`{k}`" for k in cat.exclude_keywords) or "--"
        rid = ", ".join(f"`{r}`" for r in cat.rule_ids) or "--"
        lines.append(f"| {cat.name} | {kw} | {ex} | {rid} |")
    lines.append(
        "| char_limit | requires BOTH a `char`/`character(s)` mention AND an `exceed*`/`over`/`too long` "
        "violation word | -- | field-detected: `short description` -> `lint.seo.short_over_limit`, "
        "`long description` -> `lint.seo.long_over_limit`, `title` -> `lint.seo.title_over_limit`, "
        "none of those names -> no rule_id (always lands in llm_only_deterministic) |"
    )
    return "\n".join(lines)


def _aggregate_table_markdown(aggregate: dict[str, Any]) -> str:
    totals = aggregate["totals"]
    lines = [
        "| Cell | Count |",
        "|---|---|",
        f"| both_caught | {totals['both_caught']} |",
        f"| lint_only | {totals['lint_only']} |",
        f"| llm_only_deterministic | {totals['llm_only_deterministic']} |",
        f"| llm_semantic (out of lint scope) | {totals['llm_semantic']} |",
        "",
        "By phase:",
        "",
        "| Phase | both_caught | lint_only | llm_only_deterministic | llm_semantic |",
        "|---|---|---|---|---|",
    ]
    for phase in CANONICAL_PHASES:
        p = aggregate["by_phase"][phase]
        lines.append(
            f"| {phase} | {p['both_caught']} | {p['lint_only']} | {p['llm_only_deterministic']} | {p['llm_semantic']} |"
        )
    lines.append("")
    lines.append("By deterministic category:")
    lines.append("")
    lines.append("| Category | both_caught | llm_only_deterministic |")
    lines.append("|---|---|---|")
    for cat, counts in sorted(aggregate["by_category"].items()):
        lines.append(f"| {cat} | {counts['both_caught']} | {counts['llm_only_deterministic']} |")
    return "\n".join(lines)


def _per_job_table_markdown(job_matrices: list[JobMatrix]) -> str:
    lines = [
        "| Job | Status | Content type | Duration (min) | Validation result | both_caught | lint_only | llm_only_det | llm_semantic |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for jm in sorted(job_matrices, key=lambda j: j.job_id):
        bc = sum(len(c.both_caught) for c in jm.phases.values())
        lo = sum(len(c.lint_only) for c in jm.phases.values())
        lod = sum(len(c.llm_only_deterministic) for c in jm.phases.values())
        sem = sum(len(c.llm_flags_semantic) for c in jm.phases.values())
        vr = "present" if jm.validation_result_present else "null"
        duration = f"{jm.duration_minutes:.1f}" if jm.duration_minutes is not None else "--"
        lines.append(
            f"| {jm.job_id} | {jm.status} | {jm.content_type} | {duration} | {vr} | {bc} | {lo} | {lod} | {sem} |"
        )
    return "\n".join(lines)


def _llm_only_deterministic_section_markdown(job_matrices: list[JobMatrix]) -> str:
    lines: list[str] = []
    any_items = False
    for jm in sorted(job_matrices, key=lambda j: j.job_id):
        for phase in CANONICAL_PHASES:
            comp = jm.phases[phase]
            for item in comp.llm_only_deterministic:
                any_items = True
                cats = ", ".join(item["categories"])
                lines.append(f"- **Job {jm.job_id} / {phase}** (categories: {cats})")
                lines.append(f'  > "{item["text"]}"')
                lines.append("  - _Analysis: TODO -- lint gap, LLM hallucination, or stale-limit mismatch?_")
    if not any_items:
        lines.append(
            "_None -- every deterministic-category LLM flag in this sample had a corresponding lint violation._"
        )
    return "\n".join(lines)


def _lint_only_candidates_markdown(job_matrices: list[JobMatrix], limit: int = 5) -> str:
    lines: list[str] = []
    count = 0
    for jm in sorted(job_matrices, key=lambda j: j.job_id):
        for phase in CANONICAL_PHASES:
            comp = jm.phases[phase]
            for item in comp.lint_only:
                if count >= limit:
                    return "\n".join(lines)
                count += 1
                lines.append(f"{count}. **Job {jm.job_id} / {phase}** -- `{item['rule_id']}` ({item['severity']})")
                lines.append(f'   > "{item["message"]}"')
                lines.append("   - _Judgment: TODO -- true positive or false positive?_")
    if count == 0:
        lines.append("_No lint_only violations in this sample._")
    return "\n".join(lines)


def write_study_md(path: Path, meta: dict[str, Any], job_matrices: list[JobMatrix], aggregate: dict[str, Any]) -> None:
    total_jobs = len(job_matrices)
    with_vr = sum(1 for jm in job_matrices if jm.validation_result_present)

    content = f"""# Stage 2 -- Lint agreement study over production jobs

Generated {meta["generated_at"]} against `{meta["base_url"]}` (read-only GETs, 0.2s
between requests). {total_jobs} jobs studied, {with_vr} with a stored
`validation_result`. House style rules: `{meta["rules_path"]}`.

## Methodology

`scripts/lint_agreement_study.py` pulls each studied job's record (for
`duration_minutes`, `content_type`, `transcript_file`, and the stored
`validation_result`) plus its `analyst_output.md` / `formatter_output.md` /
`seo_output.md` artifacts from the production API, builds the same context
dict the worker bus assembles, and runs
`api.services.style_engine.lint.run_lint` against the real
`config/house_style.yaml`. The lint suite is wired but OFF in production
(`routing.style_engine.qa_gate.merge_flags` defaults false) -- every
`validation_result` in this sample is the LLM validator's verdict alone,
untouched by lint. The two arms are independent.

### Graceful degradation (no transcript text)

The REST API has no transcript-fetch endpoint, so raw transcript text is
never available to this study -- only `transcript_file` (the filename) is
passed through in context, unused. This turns out not to matter for any
currently-implemented lint check: `run_lint` reads `analyst_output`,
`formatter_output`, `seo_output`, `duration_minutes`, `content_type`, and
`program` from its context -- never `transcript` -- so nothing degrades.
`program` is also never populated here (not present on the Job record /
queue payload); `StyleRules.limits_for()`'s `program` argument is a
documented no-op today, so this has no effect on the limits actually
applied.

### Deterministic-category keyword map

Each LLM validator flag string is classified as zero-or-more deterministic
categories (substring match, case-insensitive) or SEMANTIC (no category
matched -- content-accuracy/relevance/quality judgment, out of lint's
scope by design):

{_category_keyword_map_markdown()}

The `speaker_label_format` category explicitly EXCLUDES flags that also
mention attribution/ambiguity/unresolved-identity language, because those
are judgments about whether a specific line of dialogue was assigned to the
right speaker (semantic -- requires understanding transcript content) as
opposed to judgments about label FORMAT (single-word label, honorific,
same person spelled two inconsistent ways) which is what
`lint.formatter.speaker_label_inconsistent` actually checks.

### Matrix cells

- **both_caught** -- a deterministic-category LLM flag whose rule_id family
  has a corresponding lint violation on the same phase.
- **lint_only** -- a lint violation with no corresponding deterministic LLM
  flag.
- **llm_only_deterministic** -- a deterministic-category LLM flag lint
  missed. The critical cell for the Stage-2 acceptance criterion.
- **llm_semantic** -- LLM flags outside lint's scope, listed for context
  only (not part of the acceptance criterion).

Correspondence is at rule_id-family granularity (one deterministic LLM flag
claims at most one still-unclaimed lint violation of a matching rule_id per
phase), not exact text matching -- see `compare_phase()`'s docstring in the
script.

## Aggregate matrix

{_aggregate_table_markdown(aggregate)}

## Per-job matrix

{_per_job_table_markdown(job_matrices)}

## llm_only_deterministic -- every miss, verbatim

{_llm_only_deterministic_section_markdown(job_matrices)}

## lint_only spot-check (5 picks)

{_lint_only_candidates_markdown(job_matrices, limit=5)}

## Conclusion

TODO -- fill in after reviewing the llm_only_deterministic analysis above:
does lint catch >=100% of LLM-caught deterministic-category failures in
this sample? What's left as follow-up work vs. evidence that the LLM
validator itself is unreliable on these categories?
"""
    path.write_text(content)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--jobs", default="all", help='"all" or comma-separated job ids')
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--refresh", action="store_true", help="re-download even if cached")
    parser.add_argument("--out", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--rules", default=str(DEFAULT_RULES_PATH))
    args = parser.parse_args(argv)

    cache_dir = Path(args.cache_dir)
    out_dir = Path(args.out)

    try:
        rules = load_rules(args.rules)
    except StyleRulesError as exc:
        print(f"BLOCKED: could not load house style rules at {args.rules}: {exc}", file=sys.stderr)
        return 2

    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - environment issue, not logic
        print(f"BLOCKED: httpx not available: {exc}", file=sys.stderr)
        return 2

    try:
        with httpx.Client(timeout=15.0) as client:
            try:
                queue_jobs = fetch_queue_cached(client, args.base_url, cache_dir, args.refresh)
            except httpx.HTTPError as exc:
                print(f"BLOCKED: {args.base_url} unreachable ({exc}). Not fabricating results.", file=sys.stderr)
                return 2

            if args.jobs == "all":
                job_ids = select_eligible_jobs(queue_jobs)
            else:
                job_ids = [int(x.strip()) for x in args.jobs.split(",") if x.strip()]

            job_matrices: list[JobMatrix] = []
            for job_id in job_ids:
                job, outputs = fetch_job_bundle(client, args.base_url, job_id, cache_dir, args.refresh)
                context = build_context(job, outputs)
                lint_results = run_lint(context, rules)
                jm = build_job_matrix(
                    job_id=job_id,
                    status=job.get("status"),
                    content_type=job.get("content_type"),
                    duration_minutes=job.get("duration_minutes"),
                    lint_results=lint_results,
                    validation_result=job.get("validation_result"),
                )
                job_matrices.append(jm)
    except httpx.HTTPError as exc:
        print(f"BLOCKED: network error against {args.base_url}: {exc}. Not fabricating results.", file=sys.stderr)
        return 2

    aggregate = aggregate_matrices(job_matrices)

    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url,
        "jobs_requested": args.jobs,
        "jobs_studied": [jm.job_id for jm in job_matrices],
        "jobs_with_validation_result": sum(1 for jm in job_matrices if jm.validation_result_present),
        "rules_path": args.rules,
        "cache_dir": str(cache_dir),
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    write_agreement_json(out_dir / "agreement.json", meta, job_matrices, aggregate)
    write_study_md(out_dir / "study.md", meta, job_matrices, aggregate)

    print(f"Studied {len(job_matrices)} jobs. Wrote {out_dir / 'agreement.json'} and {out_dir / 'study.md'}")
    print(f"Aggregate: {aggregate['totals']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
