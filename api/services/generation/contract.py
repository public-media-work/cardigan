"""The generator contract (#427).

Two kinds of declared unit, both plain registry entries:

``SliceSpec``
    builds one slice of a job's context bundle. Deterministic slices are
    scripts; inferred slices call a model once per job.
``GeneratorSpec``
    produces exactly one item. It reads only the slices it declares and the
    items it depends on, and returns a :class:`~api.services.items.Proposal`.

The runner, not the unit, owns everything that isn't judgement: run order,
the slices a unit may see, caps, provenance (the *resolved* model id, tokens,
cost), and turning a failure into an item state. A unit raises
:class:`GenerationFailure` with a reason; it never pauses a job.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping, Optional

from api.models.item import CopyMode, GenerationState, Origin

if TYPE_CHECKING:  # pragma: no cover
    from api.models.bundle import BundlePart
    from api.models.item import Item
    from api.models.job import Job
    from api.services.items import Proposal


class GenerationFailure(Exception):
    """A unit could not produce its output. ``reason`` becomes the item's or
    slice's ``generation_reason`` — e.g. ``malformed_output``, ``cap:cost``."""

    def __init__(self, reason: str, detail: str = "", state: GenerationState = GenerationState.failed):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail
        self.state = state


class SliceNotDeclared(GenerationFailure):
    """A unit asked for a slice it didn't declare in ``reads`` — a contract bug."""

    def __init__(self, unit: str, slice_name: str):
        super().__init__("contract:reads", f"{unit} read undeclared slice '{slice_name}'")


@dataclass(frozen=True)
class Caps:
    """Spend limits for one unit's model call.

    ``max_output_tokens`` is sent as ``max_tokens``. ``max_cost_usd`` is
    checked *before* the call against the worst case (estimated input plus the
    full output allowance), so a capped call is never paid for.
    """

    max_cost_usd: Optional[float] = None
    max_output_tokens: Optional[int] = None


@dataclass(frozen=True)
class SliceResult:
    """What a slice builder returns. Provenance is stamped by the runner."""

    content: Any
    state: GenerationState = GenerationState.ready
    reason: Optional[str] = None


@dataclass(frozen=True)
class SliceSpec:
    """Declares one bundle slice and how it is built."""

    slice: str
    origin: Origin
    build: Callable[["BundleView", "UnitContext"], Awaitable[SliceResult]]
    reads: tuple[str, ...] = ()
    model: Optional[str] = None  # None for deterministic slices
    caps: Caps = field(default_factory=Caps)
    rebuildable: bool = True  # False when the builder needs raw job inputs (captions)


@dataclass(frozen=True)
class GeneratorSpec:
    """Declares one generator: exactly one item."""

    item: str
    origin: Origin
    run: Callable[["BundleView", Mapping[str, "Item"], "UnitContext"], Awaitable["Proposal"]]
    reads: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    model: Optional[str] = None  # None for deterministic generators
    caps: Caps = field(default_factory=Caps)


@dataclass(frozen=True)
class ResolvedSettings:
    """A unit's settings after code defaults and instance config are merged."""

    model: Optional[str]
    backend: Optional[str]
    caps: Caps
    enabled: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "backend": self.backend,
            "enabled": self.enabled,
            "max_cost_usd": self.caps.max_cost_usd,
            "max_output_tokens": self.caps.max_output_tokens,
        }


class BundleView:
    """The slices one unit may read — and nothing else.

    ``get`` returns the slice's effective content (the editor's correction
    when there is one). Asking for an undeclared slice raises
    :class:`SliceNotDeclared`, so ``reads`` is enforced rather than advisory.
    """

    def __init__(self, unit: str, parts: Mapping[str, "BundlePart"], allowed: tuple[str, ...]):
        self._unit = unit
        self._parts = {name: parts[name] for name in allowed if name in parts}
        self._allowed = frozenset(allowed)

    def get(self, slice_name: str) -> Any:
        if slice_name not in self._allowed:
            raise SliceNotDeclared(self._unit, slice_name)
        part = self._parts.get(slice_name)
        return part.effective if part is not None else None

    def revisions(self) -> dict[str, int]:
        return {name: part.revision for name, part in self._parts.items()}


@dataclass
class LLMCall:
    """One model call's provenance, as the backend reported it."""

    content: str
    model: str  # the resolved id
    backend: str  # the configured backend name, e.g. 'openrouter' or 'local-llm'
    input_tokens: int
    output_tokens: int
    cost_usd: float


class UnitContext:
    """What a unit may use besides its slices: the job, the copy mode, raw
    job inputs (captions only) and a capped model call."""

    def __init__(
        self,
        *,
        unit: str,
        job: "Job",
        copy_mode: CopyMode,
        settings: ResolvedSettings,
        llm: Any = None,
        inputs: Optional[Mapping[str, Any]] = None,
    ):
        self.unit = unit
        self.job = job
        self.copy_mode = copy_mode
        self.settings = settings
        self.inputs = dict(inputs or {})
        self._llm = llm
        self.calls: list[LLMCall] = []

    async def complete(self, system: str, user: str) -> LLMCall:
        """One capped chat call. Failures come back as GenerationFailure."""
        from api.services.llm import (
            CostCapExceededError,
            ModelNotAllowedError,
            ModelRefusalError,
            OutputTruncatedError,
            calculate_cost,
        )

        if self._llm is None:
            raise GenerationFailure("model_error", "no LLM client available")
        model = self.settings.model
        if not model:
            raise GenerationFailure("contract:model", f"{self.unit} called a model but declares none")

        caps = self.settings.caps
        if caps.max_cost_usd is not None:
            estimated_input = (len(system) + len(user)) // 4
            worst_case = calculate_cost(model, estimated_input, caps.max_output_tokens or 4096)
            if worst_case > caps.max_cost_usd:
                raise GenerationFailure(
                    "cap:cost", f"worst case ${worst_case:.4f} exceeds the ${caps.max_cost_usd:.4f} cap"
                )

        kwargs: dict[str, Any] = {}
        if caps.max_output_tokens:
            kwargs["max_tokens"] = caps.max_output_tokens

        try:
            response = await self._llm.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                backend=self.settings.backend,
                model=model,
                job_id=self.job.id,
                phase=self.unit,
                **kwargs,
            )
        except ModelRefusalError as err:
            raise GenerationFailure("model_refusal", str(err)) from err
        except OutputTruncatedError as err:
            raise GenerationFailure("cap:output_tokens", str(err)) from err
        except CostCapExceededError as err:
            raise GenerationFailure("cap:cost", str(err)) from err
        except ModelNotAllowedError as err:
            raise GenerationFailure("model_not_allowed", str(err)) from err
        except GenerationFailure:
            raise
        except Exception as err:
            raise GenerationFailure("model_error", str(err)) from err

        call = LLMCall(
            content=response.content or "",
            model=response.model or model,
            backend=self._llm.backend_name(self.settings.backend),
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_usd=response.cost,
        )
        self.calls.append(call)
        return call

    def provenance(self) -> dict[str, Any]:
        """Totals across this unit's calls, for the runner to stamp."""
        if not self.calls:
            return {"model": None, "backend": None, "input_tokens": None, "output_tokens": None, "cost_usd": None}
        return {
            "model": self.calls[-1].model,
            "backend": self.calls[-1].backend,
            "input_tokens": sum(c.input_tokens for c in self.calls),
            "output_tokens": sum(c.output_tokens for c in self.calls),
            "cost_usd": sum(c.cost_usd for c in self.calls),
        }


_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse a model's JSON object, tolerating a code fence or stray prose.

    Raises GenerationFailure('malformed_output') when no object can be read.
    """
    candidate = text.strip()
    fenced = _FENCE_RE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    if not candidate.startswith("{"):
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            raise GenerationFailure("malformed_output", "no JSON object in the response")
        candidate = candidate[start : end + 1]
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as err:
        raise GenerationFailure("malformed_output", str(err)) from err
    if not isinstance(value, dict):
        raise GenerationFailure("malformed_output", "the response is not a JSON object")
    return value
