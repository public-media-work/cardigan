"""The generation runner (#427): build the bundle, then run every generator.

Rules it enforces, so units don't have to:

- **Order.** Slices build in ``reads`` order, generators in ``depends_on``
  order.
- **Reads.** A unit sees only the slices it declares (``BundleView``).
- **A job never pauses.** A unit's failure becomes its own state with a
  reason. It cascades only through declarations: a generator that reads a
  slice that isn't ready is ``unavailable``/``slice:<name>``; one whose
  dependency isn't ready is ``unavailable``/``dependency:<key>``.
- **No retries** beyond the LLM client's own transport retries and escalation.
- **Provenance.** The runner stamps the resolved model id, tokens and cost.
- **Re-runs are explicit.** ``rebuild_slice`` and ``regenerate_item`` re-run
  exactly one unit; whatever read the old output shows as stale.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, Iterable, Mapping, Optional, Sequence, TypeVar

from api.models.bundle import BundlePart
from api.models.item import CopyMode, GenerationState, Item
from api.services.database import (
    get_bundle_parts,
    get_job,
    get_job_item,
    upsert_job_items,
    write_bundle_part,
)
from api.services.generation import settings as gen_settings
from api.services.generation.contract import (
    BundleView,
    GenerationFailure,
    GeneratorSpec,
    SliceSpec,
    UnitContext,
)
from api.services.generation.generators import GENERATOR_SPECS
from api.services.generation.slices import SLICE_SPECS, known_text, sst_context_for_items
from api.services.items import ITEM_SPECS, SPECS_BY_KEY, Proposal, build_items

logger = logging.getLogger(__name__)

T = TypeVar("T", SliceSpec, GeneratorSpec)

SLICES_BY_NAME: dict[str, SliceSpec] = {spec.slice: spec for spec in SLICE_SPECS}
GENERATORS_BY_ITEM: dict[str, GeneratorSpec] = {spec.item: spec for spec in GENERATOR_SPECS}


def _topological(specs: Sequence[T], name_of, deps_of) -> list[T]:
    """Order specs so each comes after what it depends on. Raises on a cycle."""
    by_name = {name_of(spec): spec for spec in specs}
    ordered: list[T] = []
    state: dict[str, int] = {}

    def visit(name: str) -> None:
        if state.get(name) == 2:
            return
        if state.get(name) == 1:
            raise ValueError(f"dependency cycle through {name!r}")
        state[name] = 1
        for dep in deps_of(by_name[name]):
            if dep in by_name:
                visit(dep)
        state[name] = 2
        ordered.append(by_name[name])

    for spec in specs:
        visit(name_of(spec))
    return ordered


SLICE_ORDER = _topological(SLICE_SPECS, lambda s: s.slice, lambda s: s.reads)
GENERATOR_ORDER = _topological(GENERATOR_SPECS, lambda g: g.item, lambda g: g.depends_on)


def resolved_settings(llm_config: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """Every unit's effective settings — what a job records that it ran with."""
    return {
        "engine": gen_settings.engine(llm_config),
        "slices": {spec.slice: gen_settings.resolve(spec, llm_config).as_dict() for spec in SLICE_SPECS},
        "generators": {spec.item: gen_settings.resolve(spec, llm_config).as_dict() for spec in GENERATOR_SPECS},
    }


# ---------------------------------------------------------------------------
# Slices
# ---------------------------------------------------------------------------


async def _build_slice(
    spec: SliceSpec,
    job: Any,
    parts: dict[str, BundlePart],
    *,
    copy_mode: CopyMode,
    llm: Any,
    llm_config: Optional[Mapping[str, Any]],
    inputs: Mapping[str, Any],
) -> BundlePart:
    settings = gen_settings.resolve(spec, llm_config)
    write = dict(origin=spec.origin.value, producer=spec.slice)

    blocked = next((name for name in spec.reads if name not in parts or not parts[name].is_ready()), None)
    if blocked is not None:
        part = await write_bundle_part(
            job.id, spec.slice, state=GenerationState.unavailable.value, reason=f"slice:{blocked}", **write
        )
    elif not settings.enabled:
        part = await write_bundle_part(
            job.id, spec.slice, state=GenerationState.unavailable.value, reason="slice_disabled", **write
        )
    else:
        ctx = UnitContext(
            unit=f"slice:{spec.slice}", job=job, copy_mode=copy_mode, settings=settings, llm=llm, inputs=inputs
        )
        try:
            result = await spec.build(BundleView(spec.slice, parts, spec.reads), ctx)
            part = await write_bundle_part(
                job.id,
                spec.slice,
                state=result.state.value,
                reason=result.reason,
                content=result.content,
                **write,
                **ctx.provenance(),
            )
        except GenerationFailure as failure:
            logger.warning("Slice failed", extra={"job_id": job.id, "slice": spec.slice, "reason": failure.reason})
            part = await write_bundle_part(
                job.id, spec.slice, state=failure.state.value, reason=failure.reason, **write, **ctx.provenance()
            )
        except Exception as err:
            logger.error(
                "Slice builder crashed", extra={"job_id": job.id, "slice": spec.slice, "error": str(err)}, exc_info=True
            )
            part = await write_bundle_part(
                job.id, spec.slice, state=GenerationState.failed.value, reason="builder_error", **write
            )

    parts[spec.slice] = part
    return part


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------


def _missing(spec: GeneratorSpec, reason: str, state: GenerationState = GenerationState.unavailable) -> Proposal:
    return Proposal(value=None, origin=spec.origin, generator=spec.item, missing_reason=reason, missing_state=state)


def _has_basis(item_key: str, parts: Mapping[str, BundlePart]) -> bool:
    field_id = SPECS_BY_KEY[item_key].sst_field_id
    sst = parts.get("sst")
    if not field_id or sst is None or not sst.is_ready():
        return False
    value = ((sst.effective or {}).get("fields") or {}).get(field_id)
    return value is not None and bool(str(value).strip())


async def _run_generator(
    spec: GeneratorSpec,
    job: Any,
    parts: Mapping[str, BundlePart],
    *,
    copy_mode: CopyMode,
    llm: Any,
    llm_config: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    """Run one generator and persist its item. Never raises for a unit failure."""
    settings = gen_settings.resolve(spec, llm_config)
    item_spec = SPECS_BY_KEY[spec.item]
    is_copy = item_spec.category.value == "copy"
    revisions: dict[str, Any] = {"slices": {}, "items": {}}
    deps: dict[str, Item] = {}
    proposal: Optional[Proposal] = None

    blocked = next((name for name in spec.reads if name not in parts or not parts[name].is_ready()), None)
    if blocked is not None:
        proposal = _missing(spec, f"slice:{blocked}")
    if proposal is None:
        for key in spec.depends_on:
            dep = await get_job_item(job.id, key)
            if dep is None or dep.generation_state != GenerationState.ready:
                proposal = _missing(spec, f"dependency:{key}")
                break
            deps[key] = dep
            revisions["items"][key] = dep.generated_at.isoformat() if dep.generated_at else None
    if proposal is None and not settings.enabled:
        proposal = _missing(spec, "generator_disabled")

    # Copy items that copy_generation rules out are left for build_items to
    # label (copy_generation_off / copy_review_no_basis) — no model call.
    skip_copy = is_copy and (
        copy_mode == CopyMode.off or (copy_mode == CopyMode.review and not _has_basis(spec.item, parts))
    )

    if proposal is None and not skip_copy:
        revisions["slices"] = {name: parts[name].revision for name in spec.reads}
        ctx = UnitContext(unit=f"generator:{spec.item}", job=job, copy_mode=copy_mode, settings=settings, llm=llm)
        try:
            proposal = await spec.run(BundleView(spec.item, parts, spec.reads), deps, ctx)
        except GenerationFailure as failure:
            logger.warning("Generator failed", extra={"job_id": job.id, "item": spec.item, "reason": failure.reason})
            proposal = _missing(spec, failure.reason, failure.state)
        except Exception as err:
            logger.error(
                "Generator crashed", extra={"job_id": job.id, "item": spec.item, "error": str(err)}, exc_info=True
            )
            proposal = _missing(spec, "generator_error", GenerationState.failed)

        provenance = ctx.provenance()
        proposal = dataclasses.replace(
            proposal,
            generator=proposal.generator or spec.item,
            model=provenance["model"] or proposal.model,
            backend=provenance["backend"] or proposal.backend,
            input_tokens=provenance["input_tokens"],
            output_tokens=provenance["output_tokens"],
            cost_usd=provenance["cost_usd"],
        )

    job_part = parts.get("job")
    content_type = ((job_part.effective if job_part else None) or {}).get("content_type") or "full"
    sst_part = parts.get("sst")
    rows = build_items(
        job_id=job.id,
        proposals={spec.item: proposal} if proposal is not None else {},
        sst_context=sst_context_for_items(sst_part.effective if sst_part and sst_part.is_ready() else None),
        copy_generation=copy_mode,
        content_type=content_type,
        known_text=_known_text(parts),
    )
    row = next(r for r in rows if r["key"] == spec.item)
    row["input_revisions"] = revisions
    await upsert_job_items(job.id, [row])
    return row


def _known_text(parts: Mapping[str, BundlePart]) -> str:
    """The job's source text — captions, SST record, glossary — for grounding checks."""
    sources = [parts[name].effective for name in ("captions", "sst", "house_rules") if name in parts]
    return known_text(*sources)


async def _write_ungenerated_items(job: Any, parts: Mapping[str, BundlePart], copy_mode: CopyMode) -> None:
    """Items with no generator yet still exist — unavailable, with their basis."""
    keys = {spec.key for spec in ITEM_SPECS} - set(GENERATORS_BY_ITEM)
    if not keys:
        return
    job_part = parts.get("job")
    sst_part = parts.get("sst")
    rows = build_items(
        job_id=job.id,
        proposals={},
        sst_context=sst_context_for_items(sst_part.effective if sst_part and sst_part.is_ready() else None),
        copy_generation=copy_mode,
        content_type=((job_part.effective if job_part else None) or {}).get("content_type") or "full",
    )
    await upsert_job_items(job.id, [row for row in rows if row["key"] in keys])


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


async def _load_job(job_id: int) -> Any:
    job = await get_job(job_id)
    if job is None:
        raise LookupError(f"job {job_id} not found")
    return job


async def run_job(
    job_id: int,
    *,
    llm: Any,
    llm_config: Optional[Mapping[str, Any]],
    inputs: Mapping[str, Any],
) -> dict[str, Any]:
    """Build every slice, then run every generator. Returns a summary.

    ``inputs`` carries what only a fresh run has: ``transcript_text`` and
    ``srt_text`` for the captions slice, and ``pin_sst``.
    """
    job = await _load_job(job_id)
    copy_mode = gen_settings.job_copy_mode(job, llm_config)
    run_inputs = {**inputs, "resolved_settings": resolved_settings(llm_config)}

    parts: dict[str, BundlePart] = {}
    for spec in SLICE_ORDER:
        await _build_slice(spec, job, parts, copy_mode=copy_mode, llm=llm, llm_config=llm_config, inputs=run_inputs)

    rows = []
    for spec in GENERATOR_ORDER:
        rows.append(await _run_generator(spec, job, parts, copy_mode=copy_mode, llm=llm, llm_config=llm_config))
    await _write_ungenerated_items(job, parts, copy_mode)

    summary = {
        "job_id": job_id,
        "copy_generation": copy_mode.value,
        "slices": {name: part.state.value for name, part in parts.items()},
        "items": {row["key"]: row["generation_state"] for row in rows},
    }
    logger.info("Generation run finished", extra=summary)
    return summary


async def rebuild_slice(
    job_id: int, slice_name: str, *, llm: Any, llm_config: Optional[Mapping[str, Any]]
) -> BundlePart:
    """Re-run one slice builder. Items that read the old revision go stale."""
    spec = SLICES_BY_NAME.get(slice_name)
    if spec is None:
        raise KeyError(slice_name)
    if not spec.rebuildable:
        raise ValueError(f"slice '{slice_name}' needs the job's raw inputs; re-run the job instead")
    job = await _load_job(job_id)
    parts = {part.slice: part for part in await get_bundle_parts(job_id)}
    copy_mode = gen_settings.job_copy_mode(job, llm_config)
    inputs = {"resolved_settings": resolved_settings(llm_config)}
    return await _build_slice(spec, job, parts, copy_mode=copy_mode, llm=llm, llm_config=llm_config, inputs=inputs)


async def regenerate_item(job_id: int, key: str, *, llm: Any, llm_config: Optional[Mapping[str, Any]]) -> Item:
    """Re-run one generator against the current bundle. Dependents go stale."""
    spec = GENERATORS_BY_ITEM.get(key)
    if spec is None:
        raise KeyError(key)
    job = await _load_job(job_id)
    parts = {part.slice: part for part in await get_bundle_parts(job_id)}
    copy_mode = gen_settings.job_copy_mode(job, llm_config)
    await _run_generator(spec, job, parts, copy_mode=copy_mode, llm=llm, llm_config=llm_config)
    item = await get_job_item(job_id, key)
    assert item is not None
    return item


def mark_stale(items: Iterable[Item], parts: Iterable[BundlePart]) -> list[Item]:
    """Set ``stale`` on items whose recorded inputs have since changed.

    A slice is stale input when its revision moved on; a dependency is stale
    input when it was regenerated after this item read it.
    """
    items = list(items)
    current = {part.slice: part.revision for part in parts}
    generated = {item.key: item.generated_at.isoformat() if item.generated_at else None for item in items}

    for item in items:
        recorded = item.input_revisions or {}
        slices = recorded.get("slices") or {}
        deps = recorded.get("items") or {}
        item.stale = any(current.get(name, rev) != rev for name, rev in slices.items()) or any(
            key in generated and generated[key] != at for key, at in deps.items()
        )
    return items
