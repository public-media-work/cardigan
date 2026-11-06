"""Generators — one per item (#427).

Deterministic: the four intent dates, derived from ``Digital Premiere``.
Inferred: ``title``. Every other item ships as ``unavailable``/
``not_generated`` until its generator lands, each in its own PR.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping

from api.models.item import Item, Origin
from api.services.generation.contract import (
    BundleView,
    Caps,
    GenerationFailure,
    GeneratorSpec,
    UnitContext,
    parse_json_object,
)
from api.services.generation.slices import DEFAULT_MODEL, sst_context_for_items
from api.services.items import (
    DETERMINISTIC_DATE_KEYS,
    SPECS_BY_KEY,
    Candidate,
    Proposal,
    join_title,
    premiere_date_proposals,
    split_title,
)

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts" / "generation"
FLAG_CHECKS = frozenset({"transcript_support", "missing_entity", "house_style", "keyword"})
MAX_ALTERNATIVES = 3


# ---------------------------------------------------------------------------
# Dates (deterministic)
# ---------------------------------------------------------------------------


def _date_generator(key: str):
    async def run(view: BundleView, deps: Mapping[str, Item], ctx: UnitContext) -> Proposal:
        return premiere_date_proposals(sst_context_for_items(view.get("sst")))[key]

    run.__name__ = f"generate_{key}"
    return run


DATE_GENERATORS: tuple[GeneratorSpec, ...] = tuple(
    GeneratorSpec(item=key, origin=Origin.deterministic, run=_date_generator(key), reads=("sst",))
    for key in DETERMINISTIC_DATE_KEYS
)


# ---------------------------------------------------------------------------
# Title (inferred)
# ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def _model_flags(raw: Any) -> tuple[dict[str, Any], ...]:
    flags = []
    for flag in raw if isinstance(raw, list) else []:
        if not isinstance(flag, dict) or flag.get("check") not in FLAG_CHECKS:
            continue
        flags.append(
            {
                "check": flag["check"],
                "rule_id": f"model.{flag['check']}",
                "severity": flag.get("severity") if flag.get("severity") in ("warning", "info") else "info",
                "message": str(flag.get("message") or "").strip() or flag["check"],
                "target": flag.get("target") if flag.get("target") in ("basis", "proposal") else "basis",
            }
        )
    return tuple(flags)


def title_user_prompt(
    *, basis_part: str | None, program: str | None, mode: str, outline: Any, sst: dict, limits: dict
) -> str:
    fields = sst.get("fields") or {}
    title_limits = limits.get("title") or {}
    limit_line = ""
    if title_limits.get("max"):
        limit_line = (
            f"\n## Length\n\nThe title part must be {title_limits['max']} characters or fewer"
            + (
                f", and the whole title, program name included, {title_limits['full_max']} or fewer"
                if title_limits.get("full_max")
                else ""
            )
            + ". These are hard limits. They apply to every alternative too.\n"
        )
    short = fields.get(SPECS_BY_KEY["short_description"].sst_field_id)
    long = fields.get(SPECS_BY_KEY["long_description"].sst_field_id)
    return (
        f"## Current title\n\n{basis_part or '(empty)'}\n\n"
        f"## Program\n\n{program or '(unknown)'}\n\n"
        f"## Mode\n\n{mode}\n\n"
        f"## Outline\n\n{json.dumps(outline, indent=2, ensure_ascii=False)}\n\n"
        f"## SST descriptions\n\n**Short:** {short or '(empty)'}\n**Long:** {long or '(empty)'}\n"
        f"{limit_line}"
    )


async def generate_title(view: BundleView, deps: Mapping[str, Item], ctx: UnitContext) -> Proposal:
    job = view.get("job") or {}
    sst = view.get("sst") or {}
    outline = view.get("outline")
    rules = view.get("house_rules") or {}

    basis = (sst.get("fields") or {}).get(SPECS_BY_KEY["title"].sst_field_id)
    basis = basis.strip() if isinstance(basis, str) and basis.strip() else None
    basis_part, basis_program = split_title(basis) if basis else (None, None)
    program = job.get("program") or basis_program

    system = (PROMPTS_DIR / "title.md").read_text(encoding="utf-8")
    system = system.replace("{{style:seo.copy_rules}}", rules.get("copy_rules") or "")
    user = title_user_prompt(
        basis_part=basis_part,
        program=program,
        mode=ctx.copy_mode.value,
        outline=outline,
        sst=sst,
        limits=rules.get("limits") or {},
    )

    call = await ctx.complete(system, user)
    data = parse_json_object(call.content)

    verdict = data.get("verdict")
    rationale = str(data.get("rationale") or "").strip() or None
    flags = _model_flags(data.get("flags"))

    proposal_value = None
    if verdict == "revise":
        raw = data.get("proposal")
        if not isinstance(raw, str) or not raw.strip():
            raise GenerationFailure("malformed_output", "verdict is 'revise' but there is no proposal")
        proposal_value = join_title(raw, program)
    elif verdict != "keep":
        raise GenerationFailure("malformed_output", f"unknown verdict {verdict!r}")

    # Alternatives that repeat the basis, the proposal or each other add
    # nothing for the editor; drop them in code rather than trust the model.
    seen = {_norm(v) for v in (basis, proposal_value) if v}
    alternatives: list[Candidate] = []
    for alt in data.get("alternatives") or []:
        if not isinstance(alt, dict) or not isinstance(alt.get("value"), str) or not alt["value"].strip():
            continue
        value = join_title(alt["value"], program)
        if _norm(value) in seen:
            continue
        seen.add(_norm(value))
        angle = alt.get("angle") if isinstance(alt.get("angle"), str) else None
        alternatives.append(Candidate(value=value, angle=angle))
        if len(alternatives) == MAX_ALTERNATIVES:
            break

    return Proposal(
        value=proposal_value,
        origin=Origin.inferred,
        generator="title",
        rationale=rationale,
        affirm=verdict == "keep",
        alternatives=tuple(alternatives),
        flags=flags,
    )


TITLE_GENERATOR = GeneratorSpec(
    item="title",
    origin=Origin.inferred,
    run=generate_title,
    reads=("job", "sst", "outline", "house_rules"),
    model=DEFAULT_MODEL,
    caps=Caps(max_cost_usd=0.10, max_output_tokens=2000),
)

GENERATOR_SPECS: tuple[GeneratorSpec, ...] = DATE_GENERATORS + (TITLE_GENERATOR,)
