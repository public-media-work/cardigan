"""Generation settings: code defaults, then instance config, then the job (#427).

Instance config lives under ``routing.generation`` in ``config/llm-config.json``::

    "generation": {
      "engine": "v4",                  # v4 | items — which engine this instance runs
      "copy_generation": "suggest",    # off | review | suggest
      "backend": null,                 # null = the client's primary backend
      "generators": {"title": {"model": "...", "max_cost_usd": 0.05, "enabled": true}},
      "slices": {"outline": {"model": "..."}}
    }

Only ``copy_generation`` can be overridden per job (``jobs.copy_generation``).
Models and caps are instance-wide, so cost and provenance stay predictable.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from api.models.item import CopyMode
from api.services.generation.contract import Caps, GeneratorSpec, ResolvedSettings, SliceSpec

ENGINE_V4 = "v4"
ENGINE_ITEMS = "items"


def generation_config(llm_config: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    return ((llm_config or {}).get("routing") or {}).get("generation") or {}


def engine(llm_config: Optional[Mapping[str, Any]]) -> str:
    """Which engine this instance runs. Anything but 'items' is V4."""
    value = generation_config(llm_config).get("engine")
    return ENGINE_ITEMS if value == ENGINE_ITEMS else ENGINE_V4


def instance_copy_mode(llm_config: Optional[Mapping[str, Any]]) -> CopyMode:
    return CopyMode.parse(generation_config(llm_config).get("copy_generation"), default=CopyMode.suggest)


def job_copy_mode(job: Any, llm_config: Optional[Mapping[str, Any]]) -> CopyMode:
    """The job's override when it has one, else the instance setting."""
    override = getattr(job, "copy_generation", None)
    if override:
        return CopyMode.parse(override, default=instance_copy_mode(llm_config))
    return instance_copy_mode(llm_config)


def resolve(spec: GeneratorSpec | SliceSpec, llm_config: Optional[Mapping[str, Any]]) -> ResolvedSettings:
    """Merge a unit's code defaults with its instance config entry."""
    cfg = generation_config(llm_config)
    if isinstance(spec, GeneratorSpec):
        entry = (cfg.get("generators") or {}).get(spec.item) or {}
    else:
        entry = (cfg.get("slices") or {}).get(spec.slice) or {}

    model = entry.get("model", spec.model) if spec.model is not None else None
    caps = Caps(
        max_cost_usd=entry.get("max_cost_usd", spec.caps.max_cost_usd),
        max_output_tokens=entry.get("max_output_tokens", spec.caps.max_output_tokens),
    )
    return ResolvedSettings(
        model=model,
        backend=entry.get("backend", cfg.get("backend")),
        caps=caps,
        enabled=bool(entry.get("enabled", True)),
    )
