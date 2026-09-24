"""LLM service layer for Cardigan.

Provides unified interface for LLM API calls with cost tracking,
model selection, and event logging.
"""

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from api.models.events import EventCreate, EventData, EventType
from api.services.database import log_event
from api.services.langfuse_client import get_langfuse_client
from api.services.logging import get_logger
from api.services.secrets import get_secret

# Cost cap and safety configuration - can be overridden via environment
DEFAULT_RUN_COST_CAP = 1.0  # $1 per run max
DEFAULT_MAX_COST_PER_1K_TOKENS = 0.05  # $0.05 per 1K tokens max

# Model allowlist - if set, only these models are allowed
# Empty list means all models allowed
DEFAULT_MODEL_ALLOWLIST: List[str] = []

logger = get_logger(__name__)

# A success status carrying an unparseable body is transient upstream damage,
# not a bad request — the identical call succeeds on retry. Bounded so a
# genuinely broken backend still fails the phase instead of looping.
#
# These attempts share the caller's timeout: the worker wraps chat() in a single
# asyncio.wait_for(timeout=backend.timeout). At the 300s the formatter backends
# use, three ~70s attempts fit; on a 180s backend the last attempt may be cut
# short and surface as a timeout instead. Diagnosis survives either way because
# each malformed attempt is logged as it happens. Keep the backoff short so the
# sleeps spend as little of that shared budget as possible.
MALFORMED_BODY_MAX_ATTEMPTS = 3
MALFORMED_BODY_BACKOFF_S = (1.0, 3.0)

# How much of an unparseable body to quote in the error/log. Enough to tell
# padding from a truncated payload or an HTML error page, short enough that a
# multi-megabyte body cannot flood the logs.
MALFORMED_BODY_PREFIX_CHARS = 300

# Default completion cap when neither the caller nor the backend config sets one.
# Matches the OpenAI-compatible path's long-standing default.
DEFAULT_MAX_TOKENS = 4096


class CostCapExceededError(Exception):
    """Raised when a request would exceed the run cost cap."""

    pass


class ModelNotAllowedError(Exception):
    """Raised when a model is not in the allowlist."""

    pass


class TokenCostTooHighError(Exception):
    """Raised when a model's per-token cost exceeds the safety limit."""

    pass


class BackendUnavailableError(Exception):
    """A backend that opted into deferral (``defer_when_unavailable``) is
    temporarily unavailable — busy, loading, or refusing to load under memory
    pressure (a 503), or unreachable / too slow (connection / read timeout).

    The worker catches this to requeue the job instead of failing it. Carries
    the upstream ``detail`` (for the dashboard) and an optional ``retry_after_s``
    hint from the backend.
    """

    def __init__(self, detail: str, *, backend: Optional[str] = None, retry_after_s: Optional[int] = None):
        super().__init__(detail)
        self.detail = detail
        self.backend = backend
        self.retry_after_s = retry_after_s


class MalformedResponseError(Exception):
    """A backend returned a success status with a body that is not valid JSON.

    Seen in production as an HTTP 200 whose body was whitespace padding only:
    the upstream held the connection open with keepalive padding, then ended it
    without ever sending the payload. Transient — the identical request
    succeeds on retry — so ``chat()`` retries this a bounded number of times.

    Carries the body length and a bounded prefix, because a bare
    ``json.JSONDecodeError`` reports only an offset and nothing logged the body.
    """

    def __init__(self, detail: str, *, backend: Optional[str] = None, body_length: Optional[int] = None):
        super().__init__(detail)
        self.detail = detail
        self.backend = backend
        self.body_length = body_length


def _parse_json_body(response: "httpx.Response", *, backend: Optional[str], model: str) -> Dict[str, Any]:
    """Parse a backend's response body, or raise a diagnosable error.

    ``response.json()`` on its own reports an offset and nothing else, so a
    malformed body left no way to tell padding from a truncated payload or an
    HTML error page. Quote the body here — this is the only place that sees it.
    """
    try:
        return response.json()
    except ValueError as e:
        body = response.text or ""
        detail = (
            f"{backend or 'backend'} returned HTTP {response.status_code} with a body that is not JSON "
            f"(model={model}, {len(body)} chars, parse error: {e}). "
            f"Body starts: {body[:MALFORMED_BODY_PREFIX_CHARS]!r}"
        )
        raise MalformedResponseError(detail, backend=backend, body_length=len(body)) from e


class CreditExhaustedError(Exception):
    """OpenRouter reports insufficient credit/quota (HTTP 402 or credit body).

    Never swallowed (even in an optional phase) and never consumes a retry —
    the worker routes it to pause-and-suggest: 'add credit, then retry'.
    """

    def __init__(self, detail: str, backend: Optional[str] = None):
        super().__init__(detail)
        self.detail = detail
        self.backend = backend


class OutputTruncatedError(Exception):
    """The provider stopped generation early (``finish_reason == "length"``).

    The completion is cut off mid-output but is otherwise a well-formed 200
    response, so it would pass as a successful phase. Raising here fails the
    phase loudly instead of handing a half-written transcript downstream,
    where only the coverage ratio stood a chance of catching it (#403).
    """

    def __init__(self, detail: str, backend: Optional[str] = None, output_tokens: int = 0):
        super().__init__(detail)
        self.detail = detail
        self.backend = backend
        self.output_tokens = output_tokens


def _parse_unavailable_503(response: "httpx.Response") -> tuple[str, Optional[int], bool]:
    """Extract (detail, retry_after_s, retryable) from a 503 response.

    Tolerates today's flat ``{"detail": "..."}`` body and a future richer
    envelope ``{"error": {"retryable": bool, "retry_after_s": int, "message": ...}}``
    (a local model server's busy-signal contract). Defaults to retryable=True so
    a bare 503 from a deferrable backend is treated as "try later".
    """
    retryable = True
    retry_after_s: Optional[int] = None
    detail: Optional[str] = None

    ra = response.headers.get("retry-after")
    if ra and str(ra).isdigit():
        retry_after_s = int(ra)

    try:
        body = response.json()
    except Exception:
        body = {}

    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            if err.get("retryable") is False:
                retryable = False
            detail = err.get("message") or detail
            if err.get("retry_after_s") is not None:
                retry_after_s = err["retry_after_s"]
        if detail is None:
            detail = body.get("detail")
        if retry_after_s is None and body.get("retry_after_s") is not None:
            retry_after_s = body["retry_after_s"]

    if detail is None:
        detail = (response.text or "")[:200] or "backend returned 503"

    return detail, retry_after_s, retryable


# Pricing per 1M tokens (input/output) - updated Dec 2024
# These are fallback values; OpenRouter returns actual costs
MODEL_PRICING: Dict[str, Dict[str, float]] = {
    # OpenRouter free tier models (cheapskate preset)
    "xiaomi/mimo-v2-flash:free": {"input": 0.0, "output": 0.0},
    "mistralai/devstral-2-2512:free": {"input": 0.0, "output": 0.0},
    "deepseek/deepseek-r1-0528:free": {"input": 0.0, "output": 0.0},
    # OpenRouter models
    "google/gemini-2.0-flash-exp": {"input": 0.0, "output": 0.0},  # Free during preview
    "google/gemini-2.5-flash": {"input": 0.15, "output": 0.60},
    "google/gemini-3-flash-preview": {"input": 0.15, "output": 0.60},
    "google/gemini-3-pro-preview": {"input": 1.25, "output": 5.00},
    "google/gemini-pro-1.5": {"input": 1.25, "output": 5.00},
    "anthropic/claude-3.5-sonnet": {"input": 3.00, "output": 15.00},
    "anthropic/claude-sonnet-4.5": {"input": 3.00, "output": 15.00},
    "openai/gpt-4o": {"input": 2.50, "output": 10.00},
    "openai/gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "xai/grok-4.1-fast": {"input": 2.00, "output": 8.00},
    "moonshotai/kimi-k2-0711:free": {"input": 0.0, "output": 0.0},
    # Direct API models
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "claude-3-5-sonnet-latest": {"input": 3.00, "output": 15.00},
    # Claude via OpenRouter — tier 0/1/2 (pricing per 1M tokens)
    "anthropic/claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "anthropic/claude-haiku-4.5": {"input": 0.80, "output": 4.00},
    "anthropic/claude-sonnet-4-5-20250514": {"input": 3.00, "output": 15.00},
    "anthropic/claude-opus-4-5-20250514": {"input": 15.00, "output": 75.00},
    "anthropic/claude-sonnet-4.6": {"input": 3.00, "output": 15.00},
    "anthropic/claude-opus-4.6": {"input": 5.00, "output": 25.00},
    "gemini-1.5-flash": {"input": 0.075, "output": 0.30},
    "gemini-1.5-flash-8b": {"input": 0.0375, "output": 0.15},
    "gemini-1.5-pro": {"input": 1.25, "output": 5.00},
}


@dataclass
class LLMResponse:
    """Response from an LLM API call."""

    content: str
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost: float
    duration_ms: int
    backend: str
    raw_response: Optional[Dict[str, Any]] = None
    # Tokens spent on model reasoning. Drawn from the same max_tokens budget as
    # visible output on reasoning-by-default models, so a healthy-looking
    # completion count can contain almost no transcript (#403).
    reasoning_tokens: int = 0


@dataclass
class RunCostTracker:
    """Tracks cumulative costs for a processing run."""

    job_id: Optional[int] = None
    total_cost: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    call_count: int = 0
    calls: List[Dict[str, Any]] = field(default_factory=list)
    start_time: Optional[datetime] = None

    def add_call(self, response: LLMResponse) -> None:
        """Add an LLM call to the running totals."""
        self.total_cost += response.cost
        self.total_input_tokens += response.input_tokens
        self.total_output_tokens += response.output_tokens
        self.total_tokens += response.total_tokens
        self.call_count += 1
        self.calls.append(
            {
                "model": response.model,
                "backend": response.backend,
                "tokens": response.total_tokens,
                "cost": response.cost,
                "duration_ms": response.duration_ms,
            }
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return summary dict for logging."""
        return {
            "job_id": self.job_id,
            "total_cost": round(self.total_cost, 6),
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_tokens": self.total_tokens,
            "call_count": self.call_count,
        }


# Per-job cost trackers (keyed by job_id) — safe for concurrent processing
_run_trackers: Dict[int, RunCostTracker] = {}

# Legacy global for backward compatibility (used when job_id is None)
_current_run_tracker: Optional[RunCostTracker] = None


def start_run_tracking(job_id: Optional[int] = None) -> RunCostTracker:
    """Start tracking costs for a new processing run."""
    global _current_run_tracker
    tracker = RunCostTracker(
        job_id=job_id,
        start_time=datetime.now(timezone.utc),
    )
    if job_id is not None:
        _run_trackers[job_id] = tracker
    _current_run_tracker = tracker
    return tracker


def get_run_tracker(job_id: Optional[int] = None) -> Optional[RunCostTracker]:
    """Get a run's cost tracker by job_id."""
    if job_id is not None and job_id in _run_trackers:
        return _run_trackers[job_id]
    return _current_run_tracker


async def end_run_tracking(job_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """End run tracking and emit worker:completed event.

    Returns summary dict with total_cost and total_tokens.
    """
    global _current_run_tracker

    # Look up by job_id first, fall back to global
    tracker = None
    if job_id is not None:
        tracker = _run_trackers.pop(job_id, None)
    if tracker is None:
        tracker = _current_run_tracker
        _current_run_tracker = None

    if tracker is None:
        return None

    summary = tracker.to_dict()

    # Log worker:completed event
    await log_event(
        EventCreate(
            job_id=tracker.job_id,
            event_type=EventType.job_completed,
            data=EventData(
                cost=tracker.total_cost,
                tokens=tracker.total_tokens,
                extra={
                    "input_tokens": tracker.total_input_tokens,
                    "output_tokens": tracker.total_output_tokens,
                    "call_count": tracker.call_count,
                },
            ),
        )
    )

    # Clean up global if it was this tracker
    if _current_run_tracker is tracker:
        _current_run_tracker = None

    return summary


# Qwen3-family chain-of-thought blocks, emitted by the local MLX backend by
# default; stripped so downstream phases get clean answers.
_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
# Qwen3 chat templates often inject the opening <think> into the prompt, so the
# server streams back only the reasoning and a dangling </think> — strip that
# leading prefix too (no-op once the well-formed block above has been removed).
_DANGLING_THINK_RE = re.compile(r"\A.*?</think>\s*", re.DOTALL)
# A markdown code fence wrapping the entire response (```json ... ```), common
# when asking for structured output; stripped so results parse directly.
_FENCE_RE = re.compile(r"\A```[a-zA-Z]*\n(.*?)\n?```\s*\Z", re.DOTALL)


def strip_reasoning(text: str) -> str:
    """Remove Qwen <think> blocks and a whole-response markdown fence.

    Mirrors the-lodge `outsource.py` so the local MLX backend's output matches
    what cloud backends return. Handles both well-formed ``<think>…</think>``
    blocks and a dangling ``</think>`` with no opener (the common Qwen3 case).
    Safe on already-clean text (no-op).
    """
    text = _THINK_RE.sub("", text)
    text = _DANGLING_THINK_RE.sub("", text, count=1)
    text = text.strip()
    fence = _FENCE_RE.match(text)
    if fence:
        text = fence.group(1).strip()
    return text


def _resolve_endpoint(config: Dict[str, Any]) -> str:
    """Resolve a backend's endpoint, honoring an optional ``endpoint_env`` override.

    Lets the deploy environment supply the URL (e.g. the LXC→Mac Studio address
    for a local model server) without baking a network address into committed
    config — so the same image can be re-pointed at a different network by
    setting one env var.

    Tolerates an OpenAI-style *base* URL ending in ``/v1`` (the convention the
    ``/local-llm`` skill and oMLX use): the chat-completions path is appended so
    a bare base and a full endpoint both work.
    """
    env_var = config.get("endpoint_env")
    # `or config["endpoint"]` (not os.getenv's default) so a set-but-empty override
    # — e.g. compose's `${LOCAL_LLM_ENDPOINT:-}` — falls back to config, not "".
    endpoint = (os.getenv(env_var) or config["endpoint"]) if env_var else config["endpoint"]
    stripped = endpoint.rstrip("/")
    if stripped.endswith("/v1"):
        return stripped + "/chat/completions"
    return endpoint


def _resolve_model(config: Dict[str, Any]) -> Optional[str]:
    """Resolve a backend's served model id, honoring an optional ``model_env`` override.

    Mirrors ``_resolve_endpoint``: a single-model local server may serve a
    different model on a different network, so the deploy env can supply the id
    (``LOCAL_LLM_MODEL``) without a committed-config edit.
    """
    env_var = config.get("model_env")
    if env_var:
        # `or config.get("model")` so a set-but-empty override — e.g. compose's
        # `${LOCAL_LLM_MODEL:-}` — falls back to the config default, not "".
        return os.getenv(env_var) or config.get("model")
    return config.get("model")


def _backend_cost(config: Dict[str, Any], model: str, input_tokens: int, output_tokens: int) -> float:
    """Cost for a call, honoring a backend's declared flat cost.

    A backend may declare ``cost_per_project`` (e.g. self-hosted local inference
    is free at 0.0); that wins over the per-token estimate, which would otherwise
    bill an unknown local model id at ``calculate_cost``'s conservative cloud rate
    and could trip the run cost cap on a genuinely free run.
    """
    flat = config.get("cost_per_project")
    if flat is not None:
        return float(flat)
    return calculate_cost(model, input_tokens, output_tokens)


def calculate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    openrouter_cost: Optional[float] = None,
) -> float:
    """Calculate cost for an API call.

    Uses OpenRouter-reported cost if available, otherwise estimates
    from MODEL_PRICING table.

    Args:
        model: Model identifier
        input_tokens: Number of input tokens
        output_tokens: Number of output tokens
        openrouter_cost: Cost reported by OpenRouter (if available)

    Returns:
        Cost in USD
    """
    # Prefer OpenRouter's reported cost
    if openrouter_cost is not None:
        return openrouter_cost

    # Look up pricing
    pricing = MODEL_PRICING.get(model)
    if pricing is None:
        # Unknown model - estimate conservatively
        pricing = {"input": 1.0, "output": 3.0}  # $1/M input, $3/M output

    input_cost = (input_tokens / 1_000_000) * pricing["input"]
    output_cost = (output_tokens / 1_000_000) * pricing["output"]

    return input_cost + output_cost


class LLMClient:
    """Unified client for LLM API calls with cost tracking."""

    def __init__(self, config_path: Optional[str] = None):
        """Initialize client with config.

        Args:
            config_path: Path to llm-config.json (default: config/llm-config.json)
        """
        from api.services.config_path import resolve_config_path

        self.config_path = Path(config_path) if config_path else resolve_config_path()
        self.config = self._load_config()
        self._http_client: Optional[httpx.AsyncClient] = None

        # Track active model for health endpoint
        self.active_backend: Optional[str] = None
        self.active_model: Optional[str] = None

        # Load safety guards from env/config
        self._load_safety_config()

    def _load_safety_config(self) -> None:
        """Load cost cap and allowlist configuration from environment/config."""
        # Run cost cap (per-run maximum)
        self.run_cost_cap = float(
            os.getenv("LLM_RUN_COST_CAP", self.config.get("safety", {}).get("run_cost_cap", DEFAULT_RUN_COST_CAP))
        )

        # Max cost per 1K tokens (safety against expensive models)
        self.max_cost_per_1k_tokens = float(
            os.getenv(
                "LLM_MAX_COST_PER_1K_TOKENS",
                self.config.get("safety", {}).get("max_cost_per_1k_tokens", DEFAULT_MAX_COST_PER_1K_TOKENS),
            )
        )

        # Model allowlist
        allowlist_env = os.getenv("LLM_MODEL_ALLOWLIST", "")
        if allowlist_env:
            self.model_allowlist = [m.strip() for m in allowlist_env.split(",") if m.strip()]
        else:
            self.model_allowlist = self.config.get("safety", {}).get("model_allowlist", DEFAULT_MODEL_ALLOWLIST)

        # Whether to enforce guards (can disable for testing)
        self.enforce_guards = os.getenv("LLM_ENFORCE_GUARDS", "true").lower() == "true"

    def check_model_allowed(self, model: str) -> None:
        """Check if model is in the allowlist.

        Raises:
            ModelNotAllowedError: If model is not allowed
        """
        if not self.enforce_guards:
            return

        if not self.model_allowlist:
            return  # Empty allowlist = all models allowed

        # A backend that resolves to no model (no `model`/`model_env`/fallback) can't
        # be validated against a configured allowlist — fail closed rather than
        # AttributeError on `None.startswith(...)`.
        if not model:
            raise ModelNotAllowedError(
                "No model resolved for this backend; cannot validate against the "
                f"allowlist. Allowed: {', '.join(self.model_allowlist)}"
            )

        # Check exact match or prefix match (for versioned models)
        for allowed in self.model_allowlist:
            if model == allowed or model.startswith(allowed + ":"):
                return

        raise ModelNotAllowedError(
            f"Model '{model}' is not in allowlist. " f"Allowed: {', '.join(self.model_allowlist)}"
        )

    def check_token_cost(self, model: str) -> None:
        """Check if model's per-token cost is within safety limits.

        Raises:
            TokenCostTooHighError: If model is too expensive
        """
        if not self.enforce_guards:
            return

        pricing = MODEL_PRICING.get(model)
        if pricing is None:
            # Unknown model - be conservative and allow (but log warning)
            return

        # Calculate average cost per 1K tokens (weighted toward output)
        avg_cost_per_1k = (pricing["input"] + pricing["output"] * 2) / 3 / 1000

        if avg_cost_per_1k > self.max_cost_per_1k_tokens:
            raise TokenCostTooHighError(
                f"Model '{model}' costs ~${avg_cost_per_1k:.4f}/1K tokens, "
                f"exceeds limit of ${self.max_cost_per_1k_tokens:.4f}/1K"
            )

    def check_run_cost_cap(self) -> None:
        """Check if current run is approaching cost cap.

        Raises:
            CostCapExceededError: If cap would be exceeded
        """
        if not self.enforce_guards:
            return

        tracker = get_run_tracker()
        if tracker is None:
            return

        if tracker.total_cost >= self.run_cost_cap:
            raise CostCapExceededError(
                f"Run cost ${tracker.total_cost:.4f} has reached cap of ${self.run_cost_cap:.2f}. "
                f"Increase LLM_RUN_COST_CAP or use a cheaper model."
            )

    def _load_config(self) -> Dict[str, Any]:
        """Load LLM configuration from file."""
        if not self.config_path.exists():
            raise FileNotFoundError(f"LLM config not found: {self.config_path}")

        with open(self.config_path) as f:
            return json.load(f)

    def reload_config(self) -> None:
        """Reload configuration from file."""
        self.config = self._load_config()

    async def get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._http_client is None or self._http_client.is_closed:
            # Ensure old client is properly closed before creating new one
            if self._http_client is not None:
                try:
                    await self._http_client.aclose()
                except Exception:
                    pass  # Already broken, ignore
            self._http_client = httpx.AsyncClient(timeout=180.0)
        return self._http_client

    async def close(self) -> None:
        """Close HTTP client."""
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    def get_backend_config(self, backend_name: Optional[str] = None) -> Dict[str, Any]:
        """Get configuration for a specific backend.

        Args:
            backend_name: Backend name, or None for primary backend

        Returns:
            Backend configuration dict
        """
        if backend_name is None:
            backend_name = self.config.get("primary_backend", "openrouter")

        backends = self.config.get("backends", {})
        if backend_name not in backends:
            raise ValueError(f"Unknown backend: {backend_name}")

        return backends[backend_name]

    def get_backend_for_phase(self, phase: str) -> str:
        """Get the configured backend name for a phase.

        Returns the backend from phase_backends config, or the primary backend as fallback.
        """
        phase_backends = self.config.get("phase_backends", {})
        return phase_backends.get(phase, self.config.get("primary_backend", "openrouter"))

    # NOTE: The auto-escalation tier ladder (get_next_tier / get_escalation_config)
    # was removed in Epic L. Sprint 3 replaced automatic tier escalation with
    # user-driven retry (POST .../retry with an explicit model_override), so the
    # "cheapskate -> default -> big-brain" walk had no callers. Per-phase model
    # selection is now direct via the phase_models config (see _resolve_model in
    # generate()). See Epic L (#233) for the remaining
    # phase_backends -> phase_models consolidation.

    def get_api_key(self, backend_config: Dict[str, Any]) -> Optional[str]:
        """Get API key for a backend via the centralized secrets resolver.

        Resolves through api.services.secrets.get_secret (Docker secret file -> env
        -> macOS Keychain) instead of a bare os.getenv, so a key delivered only as a
        Docker secret or only in the Keychain still resolves even if bootstrap hasn't
        populated os.environ for this call path (#121).
        """
        key_env = backend_config.get("api_key_env")
        if key_env:
            return get_secret(key_env)
        return None

    async def chat(
        self,
        messages: List[Dict[str, str]],
        backend: Optional[str] = None,
        model: Optional[str] = None,
        job_id: Optional[int] = None,
        phase: Optional[str] = None,
        **kwargs,
    ) -> LLMResponse:
        """Make a chat completion request.

        Args:
            messages: List of message dicts with 'role' and 'content'
            backend: Backend to use (default: primary)
            model: Model override (default: phase_models config, then backend model)
            job_id: Job ID for event logging
            phase: Agent phase name for observability (analyst, formatter, etc.)
            **kwargs: Additional parameters passed to the API

        Returns:
            LLMResponse with content, tokens, and cost
        """
        backend_name = backend or self.config.get("primary_backend", "openrouter")
        backend_config = self.get_backend_config(backend_name)

        # Determine model — route on the (backend, model) pair. Priority:
        # 1. Explicit model param (caller override)
        # 2. phase_models — the per-phase assignment from the Settings UI; applies
        #    to every backend, local included, so an assigned local model id is what
        #    gets sent (no single-model force override)
        # 3. Backend's configured model, honoring a ``model_env`` override for a
        #    single-model local server, then fallback_model
        if model:
            model_id = model
        elif phase:
            phase_models = self.config.get("phase_models", {})
            model_id = phase_models.get(phase)
        else:
            model_id = None

        if not model_id:
            model_id = _resolve_model(backend_config) or backend_config.get("fallback_model")

        self.active_backend = backend_name
        self.active_model = model_id

        # Safety guards - check before making request
        self.check_run_cost_cap()
        self.check_model_allowed(model_id)
        self.check_token_cost(model_id)

        # Get API key
        api_key = self.get_api_key(backend_config)

        # Build request based on backend type
        backend_type = backend_config.get("type", "openai")

        start_time = time.time()

        async def _dispatch() -> LLMResponse:
            if backend_type == "openrouter":
                return await self._call_openrouter(backend_config, model_id, messages, api_key, **kwargs)
            if backend_type == "openai":
                return await self._call_openai(backend_config, model_id, messages, api_key, **kwargs)
            if backend_type == "anthropic":
                return await self._call_anthropic(backend_config, model_id, messages, api_key, **kwargs)
            if backend_type == "gemini":
                return await self._call_gemini(backend_config, model_id, messages, api_key, **kwargs)
            raise ValueError(f"Unsupported backend type: {backend_type}")

        # A success status with an unparseable body is transient upstream damage.
        # Retry it here rather than let it kill the phase: there is no retry
        # anywhere above this (a chunk failure fails the whole job). Deliberately
        # narrow — credit exhaustion, backend-unavailable and HTTP errors carry
        # their own recovery semantics and must not burn attempts here.
        for attempt in range(MALFORMED_BODY_MAX_ATTEMPTS):
            try:
                response = await _dispatch()
                break
            except MalformedResponseError as e:
                if attempt == MALFORMED_BODY_MAX_ATTEMPTS - 1:
                    logger.error(
                        "Malformed response — giving up after %d attempts",
                        MALFORMED_BODY_MAX_ATTEMPTS,
                        extra={
                            "job_id": job_id,
                            "phase": phase,
                            "backend": backend_name,
                            "model": model_id,
                            "attempts": MALFORMED_BODY_MAX_ATTEMPTS,
                            "body_length": e.body_length,
                            "detail": e.detail,
                        },
                    )
                    raise

                delay = MALFORMED_BODY_BACKOFF_S[min(attempt, len(MALFORMED_BODY_BACKOFF_S) - 1)]
                # An unparseable body carries no usage block, so this attempt's
                # spend cannot be added to the tracker — if the provider generated
                # tokens before the body was lost, we are billed for them blind.
                # Re-check the cap before spending again so a run that has already
                # breached it (via other calls) stops here instead of paying for
                # up to MALFORMED_BODY_MAX_ATTEMPTS more.
                logger.warning(
                    "Malformed response, retrying in %.1fs (attempt %d/%d) — this attempt's cost is untracked",
                    delay,
                    attempt + 1,
                    MALFORMED_BODY_MAX_ATTEMPTS,
                    extra={
                        "job_id": job_id,
                        "phase": phase,
                        "backend": backend_name,
                        "model": model_id,
                        "attempt": attempt + 1,
                        "max_attempts": MALFORMED_BODY_MAX_ATTEMPTS,
                        "retry_in_s": delay,
                        "body_length": e.body_length,
                        "cost_tracked": False,
                        "detail": e.detail,
                    },
                )
                self.check_run_cost_cap()
                await asyncio.sleep(delay)

        duration_ms = int((time.time() - start_time) * 1000)
        response.duration_ms = duration_ms
        response.backend = backend_name

        # Track costs (per-job tracker for concurrency safety)
        tracker = get_run_tracker(job_id)
        if tracker is not None:
            tracker.add_call(response)

        # Log cost_update event
        await log_event(
            EventCreate(
                job_id=job_id,
                event_type=EventType.cost_update,
                data=EventData(
                    cost=response.cost,
                    tokens=response.total_tokens,
                    model=response.model,
                    backend=backend_name,
                    duration_ms=duration_ms,
                ),
            )
        )

        # Send trace to Langfuse for observability
        langfuse = get_langfuse_client()
        if langfuse.is_available():
            await langfuse.trace_generation(
                name=f"{phase}-generation" if phase else "llm-generation",
                model=response.model,
                input_messages=messages,
                output=response.content,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                total_tokens=response.total_tokens,
                cost=response.cost,
                duration_ms=duration_ms,
                job_id=job_id,
                phase=phase,
                backend=backend_name,
            )

        return response

    async def _post_openrouter(
        self, endpoint: str, headers: Dict[str, str], payload: Dict[str, Any]
    ) -> "httpx.Response":
        """Seam: perform the raw HTTP POST to OpenRouter. Extracted for testability."""
        client = await self.get_client()
        return await client.post(endpoint, headers=headers, json=payload)

    async def _call_openrouter(
        self,
        config: Dict[str, Any],
        model: str,
        messages: List[Dict[str, str]],
        api_key: Optional[str],
        **kwargs,
    ) -> LLMResponse:
        """Make OpenRouter API call."""
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://pbswisconsin.org",
            "X-Title": "Cardigan",
        }

        payload = {
            "model": model,
            "messages": messages,
            **kwargs,
        }
        # Cap output explicitly. Without it OpenRouter applies its own
        # undocumented completion cap, which silently truncated the formatter
        # mid-transcript (#403). Precedence: explicit kwarg > backend config > 4096.
        payload["max_tokens"] = payload.get("max_tokens") or config.get("max_tokens", DEFAULT_MAX_TOKENS)

        response = await self._post_openrouter(config["endpoint"], headers, payload)

        # Log error details before raising
        if response.status_code >= 400:
            try:
                error_body = response.json()
            except Exception:
                error_body = {"raw": response.text[:500]}
            print(f"[LLM] OpenRouter API error status={response.status_code} model={model} error={error_body}")

            body_str = str(error_body).lower()
            if (
                response.status_code == 402
                or "insufficient" in body_str
                or ("credit" in body_str and ("exhaust" in body_str or "quota" in body_str or "balance" in body_str))
            ):
                raise CreditExhaustedError(
                    "OpenRouter credit exhausted — add credit, then retry.",
                    backend=self.active_backend,
                )

        response.raise_for_status()

        data = _parse_json_body(response, backend=self.active_backend, model=model)

        # Extract usage
        usage = data.get("usage", {})
        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)
        total_tokens = usage.get("total_tokens", input_tokens + output_tokens)
        reasoning_tokens = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0

        # OpenRouter may report cost directly
        openrouter_cost = None
        if "usage" in data and "total_cost" in data["usage"]:
            openrouter_cost = data["usage"]["total_cost"]

        # Calculate cost (force $0 for free tier models)
        actual_model = data.get("model", model)
        if actual_model.endswith(":free"):
            cost = 0.0
        else:
            cost = calculate_cost(actual_model, input_tokens, output_tokens, openrouter_cost)

        # A length stop means the provider cut generation off mid-output. The body
        # is a well-formed 200, so without this check it is recorded as a completed
        # phase and only the downstream coverage ratio can catch it (#403).
        choices = data.get("choices") or [{}]
        finish_reason = choices[0].get("finish_reason")
        if finish_reason == "length":
            raise OutputTruncatedError(
                f"Provider stopped generation early (finish_reason='length') after "
                f"{output_tokens} output tokens on model={actual_model}. The output is "
                f"truncated mid-generation; raise max_tokens or split the input.",
                backend=self.active_backend,
                output_tokens=output_tokens,
            )

        # Extract content. A 200 can still carry null content when the whole
        # completion went to reasoning — treat that as a failure rather than
        # writing an empty phase output (#403).
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content")
        if content is None:
            raise MalformedResponseError(
                f"{self.active_backend or 'openrouter'} returned HTTP 200 with null content "
                f"(model={actual_model}, finish_reason={finish_reason!r}, "
                f"{output_tokens} completion tokens of which {reasoning_tokens} were reasoning). "
                f"Nothing was generated; disable reasoning or raise max_tokens.",
                backend=self.active_backend,
                body_length=0,
            )

        return LLMResponse(
            content=content,
            model=actual_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cost=cost,
            duration_ms=0,  # Set by caller
            backend="openrouter",
            raw_response=data,
            reasoning_tokens=reasoning_tokens,
        )

    async def _call_openai(
        self,
        config: Dict[str, Any],
        model: str,
        messages: List[Dict[str, str]],
        api_key: Optional[str],
        **kwargs,
    ) -> LLMResponse:
        """Make OpenAI API call."""
        client = await self.get_client()

        headers = {"Content-Type": "application/json"}
        # Keyless backends (e.g. a local MLX server) skip auth — sending
        # "Bearer None" can trip servers/proxies that validate the header.
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload = {
            "model": model,
            "messages": messages,
            **kwargs,
        }
        # Cap output explicitly. OpenAI-compatible servers (e.g. MLX) default to a
        # tiny max_tokens (~512) that truncates analyst/formatter output; cloud
        # backends don't. Precedence: explicit kwarg > backend config > 4096.
        payload["max_tokens"] = payload.get("max_tokens") or config.get("max_tokens", 4096)
        # Local MLX (Qwen) backends: tell the server not to emit chain-of-thought
        # at all (primary control, mirroring outsource.py); strip_reasoning on the
        # response below is the belt-and-suspenders backup.
        if config.get("strip_reasoning"):
            payload["chat_template_kwargs"] = {"enable_thinking": False}

        defer = bool(config.get("defer_when_unavailable"))
        try:
            response = await client.post(
                _resolve_endpoint(config),
                headers=headers,
                json=payload,
                timeout=config.get("timeout", httpx.USE_CLIENT_DEFAULT),
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout) as e:
            # Unreachable or too slow. For a deferrable backend this is "busy /
            # not up right now" — let the worker requeue rather than fail.
            if defer:
                raise BackendUnavailableError(f"{type(e).__name__}: {e}", backend=self.active_backend) from e
            raise

        # A deferrable backend's 503 means "try later" (memory pressure / loading /
        # contention) unless it explicitly marks the error non-retryable.
        if defer and response.status_code == 503:
            detail, retry_after_s, retryable = _parse_unavailable_503(response)
            if retryable:
                raise BackendUnavailableError(detail, backend=self.active_backend, retry_after_s=retry_after_s)

        response.raise_for_status()

        data = _parse_json_body(response, backend=self.active_backend, model=model)

        usage = data.get("usage", {})
        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)
        total_tokens = usage.get("total_tokens", input_tokens + output_tokens)

        cost = _backend_cost(config, model, input_tokens, output_tokens)
        content = data["choices"][0]["message"]["content"]

        # Local MLX (Qwen) backends opt into reasoning/fence stripping so their
        # output matches what cloud backends return.
        if config.get("strip_reasoning"):
            content = strip_reasoning(content)

        return LLMResponse(
            content=content,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cost=cost,
            duration_ms=0,
            backend="openai",
            raw_response=data,
        )

    async def _call_anthropic(
        self,
        config: Dict[str, Any],
        model: str,
        messages: List[Dict[str, str]],
        api_key: Optional[str],
        **kwargs,
    ) -> LLMResponse:
        """Make Anthropic API call."""
        client = await self.get_client()

        headers = {
            "x-api-key": api_key,
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }

        # Convert messages format for Anthropic
        system_msg = None
        anthropic_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system_msg = msg["content"]
            else:
                anthropic_messages.append(msg)

        payload = {
            "model": model,
            "messages": anthropic_messages,
            "max_tokens": kwargs.get("max_tokens", 4096),
        }
        if system_msg:
            payload["system"] = system_msg

        response = await client.post(
            config["endpoint"],
            headers=headers,
            json=payload,
        )
        response.raise_for_status()

        data = _parse_json_body(response, backend=self.active_backend, model=model)

        usage = data.get("usage", {})
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        total_tokens = input_tokens + output_tokens

        cost = calculate_cost(model, input_tokens, output_tokens)
        content = data["content"][0]["text"]

        return LLMResponse(
            content=content,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cost=cost,
            duration_ms=0,
            backend="anthropic",
            raw_response=data,
        )

    async def _call_gemini(
        self,
        config: Dict[str, Any],
        model: str,
        messages: List[Dict[str, str]],
        api_key: Optional[str],
        **kwargs,
    ) -> LLMResponse:
        """Make Google Gemini API call."""
        client = await self.get_client()

        # Build endpoint with API key
        endpoint = f"{config['endpoint']}?key={api_key}"

        # Convert messages to Gemini format
        contents = []
        for msg in messages:
            role = "user" if msg["role"] in ("user", "system") else "model"
            contents.append(
                {
                    "role": role,
                    "parts": [{"text": msg["content"]}],
                }
            )

        payload = {
            "contents": contents,
            "generationConfig": {
                "maxOutputTokens": kwargs.get("max_tokens", 8192),
            },
        }

        response = await client.post(
            endpoint,
            json=payload,
        )
        response.raise_for_status()

        data = _parse_json_body(response, backend=self.active_backend, model=model)

        # Extract usage metadata
        usage = data.get("usageMetadata", {})
        input_tokens = usage.get("promptTokenCount", 0)
        output_tokens = usage.get("candidatesTokenCount", 0)
        total_tokens = usage.get("totalTokenCount", input_tokens + output_tokens)

        cost = calculate_cost(model, input_tokens, output_tokens)
        content = data["candidates"][0]["content"]["parts"][0]["text"]

        return LLMResponse(
            content=content,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cost=cost,
            duration_ms=0,
            backend="gemini",
            raw_response=data,
        )

    def get_status(self) -> Dict[str, Any]:
        """Get current LLM client status for health endpoint.

        Returns:
            Dict with active/configured backend, model, and last_run_totals
        """
        tracker = get_run_tracker()
        last_run = tracker.to_dict() if tracker else None

        # Get configured settings from primary backend
        primary_backend = self.config.get("primary_backend")
        fallback_model = None
        if primary_backend:
            backend_config = self.config.get("backends", {}).get(primary_backend, {})
            fallback_model = backend_config.get("fallback_model") or backend_config.get("model")

        # Get phase-to-backend mapping
        phase_backends = self.config.get("phase_backends", {})

        return {
            "active_backend": self.active_backend,
            "active_model": self.active_model,
            "primary_backend": primary_backend,
            "fallback_model": fallback_model,
            "phase_backends": phase_backends,
            "last_run_totals": last_run,
        }


# Global LLM client instance
_llm_client: Optional[LLMClient] = None


def get_llm_client() -> LLMClient:
    """Get or create global LLM client instance."""
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client


async def close_llm_client() -> None:
    """Close global LLM client."""
    global _llm_client
    if _llm_client is not None:
        await _llm_client.close()
        _llm_client = None
