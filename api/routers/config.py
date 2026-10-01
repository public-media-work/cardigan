"""Configuration API endpoints for Cardigan.

Provides endpoints for viewing and updating LLM routing configuration.
"""

import json
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from api.services.config_path import resolve_config_path
from api.services.cost_estimator import estimate_job_cost
from api.services.llm import get_llm_client
from api.services.model_roster import get_available_models, invalidate_cache

router = APIRouter(prefix="/config", tags=["config"])

CONFIG_PATH = resolve_config_path()


class PhaseBackendsUpdate(BaseModel):
    """Request body for updating phase-to-backend mappings."""

    phase_backends: Dict[str, str] = Field(
        ...,
        description="Mapping of phase names to backend names",
        examples=[
            {
                "analyst": "openrouter",
                "formatter": "openrouter-cheapskate",
                "seo": "openrouter-cheapskate",
                "copy_editor": "openrouter",
            }
        ],
    )


class PhaseBackendsResponse(BaseModel):
    """Response with current phase-to-backend mappings."""

    phase_backends: Dict[str, str]
    available_backends: List[str]
    available_phases: List[str]


def _load_config() -> dict:
    """Load current config from JSON file."""
    if not CONFIG_PATH.exists():
        raise HTTPException(status_code=500, detail="Config file not found")
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _save_config(config: dict) -> None:
    """Save config to JSON file with error handling."""
    try:
        # Ensure directory exists
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=2)
    except (IOError, OSError) as e:
        raise HTTPException(status_code=500, detail=f"Failed to save config: {e}")

    # Reload the LLM client's config
    llm = get_llm_client()
    llm.reload_config()


@router.get("/phase-backends", response_model=PhaseBackendsResponse)
async def get_phase_backends():
    """Get current phase-to-backend mappings.

    Returns the current configuration for which backend handles each agent phase,
    along with lists of available backends and phases.
    """
    config = _load_config()

    phase_backends = config.get("phase_backends", {})
    available_backends = list(config.get("backends", {}).keys())
    available_phases = ["analyst", "formatter", "seo", "validator", "timestamp", "copy_editor", "chat"]

    return PhaseBackendsResponse(
        phase_backends=phase_backends,
        available_backends=available_backends,
        available_phases=available_phases,
    )


@router.patch("/phase-backends", response_model=PhaseBackendsResponse)
async def update_phase_backends(update: PhaseBackendsUpdate):
    """Update phase-to-backend mappings.

    Allows reconfiguring which backend (preset tier) handles each agent phase.
    Changes are persisted to the config file and take effect immediately.
    """
    config = _load_config()
    available_backends = list(config.get("backends", {}).keys())
    valid_phases = {"analyst", "formatter", "seo", "validator", "timestamp", "copy_editor", "chat"}

    # Validate the update
    for phase, backend in update.phase_backends.items():
        if phase not in valid_phases:
            raise HTTPException(
                status_code=400, detail=f"Invalid phase: {phase}. Valid phases: {', '.join(valid_phases)}"
            )
        if backend not in available_backends:
            raise HTTPException(
                status_code=400, detail=f"Invalid backend: {backend}. Available: {', '.join(available_backends)}"
            )

    # Update config
    config["phase_backends"] = update.phase_backends
    _save_config(config)

    # Reload the live LLM client so changes take effect immediately
    from api.services.llm import get_llm_client

    get_llm_client().reload_config()

    return PhaseBackendsResponse(
        phase_backends=config["phase_backends"],
        available_backends=available_backends,
        available_phases=list(valid_phases),
    )


class AvailableModel(BaseModel):
    """A model available for phase assignment (cloud via OpenRouter or a locally
    discovered model)."""

    id: str = Field(..., description="Model ID (e.g. 'anthropic/claude-sonnet-4.6' or 'Qwen2.5-7B-Instruct-4bit')")
    name: str = Field(..., description="Human-readable model name")
    provider: str = Field(..., description="Serving provider (e.g. 'Anthropic', 'Google', 'oMLX')")
    tier: Optional[int] = Field(None, ge=0, le=2, description="Cost tier (0=economy..2=premium); null for local models")
    pricing_input: Optional[float] = Field(None, description="Cost per 1M input tokens (USD)")
    pricing_output: Optional[float] = Field(None, description="Cost per 1M output tokens (USD)")
    backend: Optional[str] = Field(None, description="Config backend key that serves this model (local models)")
    host: Optional[str] = Field(None, description="Host of the serving endpoint (local models), for grouping")
    context_len: Optional[int] = Field(None, description="Max context length if the server advertises it")


class PhaseModelsResponse(BaseModel):
    """Response with current phase-to-model assignments and available models."""

    phase_models: Dict[str, str] = Field(..., description="Current model ID assigned to each phase")
    available_models: List[AvailableModel] = Field(..., description="All models available for selection")
    available_phases: List[str] = Field(..., description="All configurable phases")


class PhaseModelsUpdate(BaseModel):
    """Request body for updating phase-to-model assignments."""

    phase_models: Dict[str, str] = Field(
        ...,
        description="Mapping of phase names to model IDs",
        examples=[
            {"analyst": "anthropic/claude-haiku-4-5-20251001", "formatter": "anthropic/claude-sonnet-4-5-20250514"}
        ],
    )


DEFAULT_PHASE_MODELS = {
    "analyst": "anthropic/claude-haiku-4.5",
    "formatter": "anthropic/claude-sonnet-4.6",
    "seo": "anthropic/claude-sonnet-4.6",
    "validator": "anthropic/claude-haiku-4.5",
    "timestamp": "anthropic/claude-sonnet-4.6",
    "copy_editor": "anthropic/claude-opus-4.6",
    "chat": "anthropic/claude-sonnet-4.6",
}


@router.get("/models", response_model=PhaseModelsResponse)
async def get_phase_models():
    """Get current phase-to-model assignments and available models.

    Returns which specific model is assigned to each agent phase,
    plus the full list of available models for the Settings UI.
    Models are fetched dynamically from OpenRouter when model_families
    is configured, falling back to the static available_models list.
    """
    config = _load_config()
    models_data = await get_available_models()
    available_models = [AvailableModel(**m) for m in models_data]
    phase_models = config.get("phase_models", DEFAULT_PHASE_MODELS)
    available_phases = ["analyst", "formatter", "seo", "validator", "timestamp", "copy_editor", "chat"]

    return PhaseModelsResponse(
        phase_models=phase_models,
        available_models=available_models,
        available_phases=available_phases,
    )


@router.patch("/models", response_model=PhaseModelsResponse)
async def update_phase_models(update: PhaseModelsUpdate):
    """Update phase-to-model assignments.

    Allows reconfiguring which specific model handles each agent phase.
    Changes are persisted to the config file and take effect immediately.
    """
    config = _load_config()
    valid_phases = {"analyst", "formatter", "seo", "validator", "timestamp", "copy_editor", "chat"}
    models_data = await get_available_models()
    # Without a roster we can neither validate the model id nor look up its serving
    # backend, so routing the (backend, model) pair below would silently leave
    # phase_backends stale — an inconsistent config. Fail loudly instead.
    if not models_data:
        raise HTTPException(
            status_code=503,
            detail="Model roster is unavailable right now; cannot assign models safely. Try again after it refreshes.",
        )
    available_model_ids = {m["id"] for m in models_data}

    for phase, model_id in update.phase_models.items():
        if phase not in valid_phases:
            raise HTTPException(
                status_code=400, detail=f"Invalid phase: {phase}. Valid phases: {', '.join(sorted(valid_phases))}"
            )
        if available_model_ids and model_id not in available_model_ids:
            raise HTTPException(status_code=400, detail=f"Unknown model: {model_id}. Check available_models in config.")

    # Merge with existing (partial update)
    phase_models = config.get("phase_models", DEFAULT_PHASE_MODELS)
    phase_models.update(update.phase_models)
    config["phase_models"] = phase_models

    # Route on the (backend, model) pair: keep phase_backends consistent with the
    # assigned model so the model actually reaches the right server. A local model
    # carries its serving backend in the roster; assigning it points the phase at
    # that backend. A cloud model has no roster backend, so only reset the phase
    # *off* a local backend (back to the primary cloud backend) — an existing cloud
    # tier (e.g. openrouter-cheapskate) is left untouched.
    roster_by_id = {m["id"]: m for m in models_data}
    phase_backends = config.get("phase_backends", {})
    backends = config.get("backends", {})
    primary = config.get("primary_backend", "openrouter")
    for phase, model_id in update.phase_models.items():
        model_backend = roster_by_id.get(model_id, {}).get("backend")
        if model_backend:
            phase_backends[phase] = model_backend
        else:
            current = phase_backends.get(phase)
            if current and backends.get(current, {}).get("type") == "openai":
                phase_backends[phase] = primary
    config["phase_backends"] = phase_backends

    _save_config(config)

    # Reload the live LLM client so changes take effect immediately
    from api.services.llm import get_llm_client

    get_llm_client().reload_config()

    return await get_phase_models()


@router.post("/models/refresh", response_model=PhaseModelsResponse)
async def refresh_model_roster():
    """Force-refresh the dynamic model roster from OpenRouter.

    Clears the cache and fetches a fresh model list. Useful when new
    models have been released and you want them immediately.
    """
    invalidate_cache()
    return await get_phase_models()


class WorkerConfigResponse(BaseModel):
    """Response with current worker configuration."""

    max_concurrent_jobs: int = Field(..., ge=1, le=5)
    poll_interval_seconds: int = Field(..., ge=1)
    heartbeat_interval_seconds: int = Field(..., ge=10)


class WorkerConfigUpdate(BaseModel):
    """Request body for updating worker configuration."""

    max_concurrent_jobs: Optional[int] = Field(None, ge=1, le=5, description="Max jobs to process concurrently (1-5)")
    poll_interval_seconds: Optional[int] = Field(None, ge=1, le=60, description="Seconds between queue polls")
    heartbeat_interval_seconds: Optional[int] = Field(None, ge=10, le=300, description="Seconds between heartbeats")


@router.get("/worker", response_model=WorkerConfigResponse)
async def get_worker_config():
    """Get current worker configuration.

    Returns settings for job processing concurrency and timing.
    """
    config = _load_config()
    worker = config.get("worker", {})

    return WorkerConfigResponse(
        max_concurrent_jobs=worker.get("max_concurrent_jobs", 3),
        poll_interval_seconds=worker.get("poll_interval_seconds", 5),
        heartbeat_interval_seconds=worker.get("heartbeat_interval_seconds", 60),
    )


@router.patch("/worker", response_model=WorkerConfigResponse)
async def update_worker_config(update: WorkerConfigUpdate):
    """Update worker configuration.

    Changes are persisted to the config file. Note: running workers will need
    to be restarted to pick up changes.
    """
    config = _load_config()
    worker = config.get(
        "worker", {"max_concurrent_jobs": 3, "poll_interval_seconds": 5, "heartbeat_interval_seconds": 60}
    )

    # Apply updates (only non-None values)
    if update.max_concurrent_jobs is not None:
        worker["max_concurrent_jobs"] = update.max_concurrent_jobs
    if update.poll_interval_seconds is not None:
        worker["poll_interval_seconds"] = update.poll_interval_seconds
    if update.heartbeat_interval_seconds is not None:
        worker["heartbeat_interval_seconds"] = update.heartbeat_interval_seconds

    # Save config
    config["worker"] = worker
    _save_config(config)

    return await get_worker_config()


class CostEstimateRequest(BaseModel):
    """Request body for cost estimation."""

    word_count: int = Field(..., ge=0, description="Transcript word count")
    phase_models: Optional[Dict[str, str]] = Field(
        None, description="Override phase-model assignments (defaults to current config)"
    )


class CostEstimateResponse(BaseModel):
    """Cost estimate for a job."""

    total_estimated_cost: float
    estimated_input_tokens: int
    phase_estimates: List[Dict[str, Any]]


@router.post("/estimate-cost", response_model=CostEstimateResponse)
async def get_cost_estimate(body: CostEstimateRequest):
    """Estimate the cost of processing a transcript.

    Uses current phase-model assignments (or overrides) and model pricing
    to estimate total cost. Useful for showing users expected costs
    before submitting a job.
    """
    config = _load_config()

    # Use provided phase_models or fall back to current config
    phase_models = body.phase_models or config.get("phase_models", DEFAULT_PHASE_MODELS)

    # Build pricing overrides from the model roster (has live OpenRouter pricing)
    pricing_overrides = {}
    try:
        models_data = await get_available_models()
        for m in models_data:
            if m.get("pricing_input") is not None and m.get("pricing_output") is not None:
                pricing_overrides[m["id"]] = {
                    "input": m["pricing_input"],
                    "output": m["pricing_output"],
                }
    except Exception:
        pass  # Fall through to MODEL_PRICING static fallback

    result = estimate_job_cost(
        word_count=body.word_count,
        phase_models=phase_models,
        pricing_overrides=pricing_overrides,
    )

    return CostEstimateResponse(**result)


# ---------------------------------------------------------------------------
# Backend-definition CRUD (/config/backends)
#
# Lets a user register a local OpenAI-compatible endpoint as a first-class,
# discoverable backend without hand-editing llm-config.json. Backends are keyed
# by host so a server self-identifies (no invented names like "local-llm-2").
# The generic OpenAI client + the model roster already consume whatever is
# written here, so onboarding a new server is data, not code.
# ---------------------------------------------------------------------------


class BackendCreate(BaseModel):
    """Request body to register a local OpenAI-compatible backend."""

    endpoint: str = Field(..., description="OpenAI-compatible base URL, e.g. http://host:8000/v1")
    api_key_env: Optional[str] = Field(None, description="Name of the secret/env var holding the API key")
    model: Optional[str] = Field(None, description="Optional fallback model id when a phase has no assignment")
    enabled: bool = Field(True, description="Whether the backend is usable")
    discover: bool = Field(True, description="Whether to list this server's /v1/models in the roster")


class BackendPatch(BaseModel):
    """Partial update for an existing backend (only provided fields change)."""

    endpoint: Optional[str] = None
    api_key_env: Optional[str] = None
    model: Optional[str] = None
    enabled: Optional[bool] = None
    discover: Optional[bool] = None


class BackendInfo(BaseModel):
    """Summary of a configured backend."""

    name: str
    type: str
    endpoint: Optional[str] = None
    enabled: bool
    discover: bool


class BackendsListResponse(BaseModel):
    backends: List[BackendInfo]


def _backend_info(name: str, entry: dict) -> BackendInfo:
    return BackendInfo(
        name=name,
        type=entry.get("type", "openai"),
        endpoint=entry.get("endpoint"),
        enabled=bool(entry.get("enabled", True)),
        discover=bool(entry.get("discover", False)),
    )


@router.get("/backends", response_model=BackendsListResponse)
async def list_backends():
    """List all configured backends (cloud presets + user-registered local endpoints)."""
    config = _load_config()
    backends = config.get("backends", {})
    return BackendsListResponse(backends=[_backend_info(n, e) for n, e in backends.items()])


@router.post("/backends", response_model=BackendInfo, status_code=201)
async def create_backend(body: BackendCreate):
    """Register a new local OpenAI-compatible backend, keyed by its host.

    The host is derived from the endpoint (self-identifying), so no name is
    invented. Cost is $0 and discovery is on by default. Invalidates the roster
    cache so the new server's models surface on the next refresh.
    """
    host = urlparse(body.endpoint).netloc
    if not host:
        raise HTTPException(status_code=400, detail="endpoint must be an absolute URL like http://host:8000/v1")

    config = _load_config()
    backends = config.setdefault("backends", {})
    if host in backends:
        raise HTTPException(status_code=409, detail=f"Backend '{host}' already exists")

    entry: Dict[str, Any] = {
        "type": "openai",
        "endpoint": body.endpoint,
        "enabled": body.enabled,
        "discover": body.discover,
        "cost_per_project": 0.0,
    }
    if body.api_key_env:
        entry["api_key_env"] = body.api_key_env
    if body.model:
        entry["model"] = body.model

    backends[host] = entry
    _save_config(config)
    invalidate_cache()
    return _backend_info(host, entry)


@router.patch("/backends/{name}", response_model=BackendInfo)
async def update_backend(name: str, body: BackendPatch):
    """Update fields of an existing backend (enable/disable, toggle discovery, edit endpoint)."""
    config = _load_config()
    backends = config.get("backends", {})
    if name not in backends:
        raise HTTPException(status_code=404, detail=f"Backend '{name}' not found")

    entry = backends[name]
    # Same absolute-URL guard as create_backend — a PATCH must not be able to point
    # a backend at a relative/garbage endpoint that create_backend would have rejected.
    if body.endpoint is not None and not urlparse(body.endpoint).netloc:
        raise HTTPException(status_code=400, detail="endpoint must be an absolute URL like http://host:8000/v1")
    for field in ("endpoint", "api_key_env", "model", "enabled", "discover"):
        value = getattr(body, field)
        if value is not None:
            entry[field] = value

    _save_config(config)
    invalidate_cache()
    return _backend_info(name, entry)


@router.delete("/backends/{name}", status_code=204)
async def delete_backend(name: str):
    """Remove a backend. Refuses if it still routes traffic (would orphan a phase)."""
    config = _load_config()
    backends = config.get("backends", {})
    if name not in backends:
        raise HTTPException(status_code=404, detail=f"Backend '{name}' not found")

    refs = []
    if config.get("primary_backend") == name:
        refs.append("primary_backend")
    if config.get("fallback_backend") == name:
        refs.append("fallback_backend")
    used_phases = [p for p, b in config.get("phase_backends", {}).items() if b == name]
    if used_phases:
        refs.append(f"phase_backends({', '.join(used_phases)})")
    if refs:
        raise HTTPException(status_code=409, detail=f"Backend '{name}' is in use by: {', '.join(refs)}")

    del backends[name]
    _save_config(config)
    invalidate_cache()
