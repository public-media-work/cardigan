"""Dynamic model roster via OpenRouter API.

Fetches available models from OpenRouter, filters by configured family
patterns, and classifies each into a cost tier. Falls back to the static
`available_models` list in llm-config.json when OpenRouter is unreachable.
"""

import asyncio
import json
import logging
import os
import time
from fnmatch import fnmatch
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from api.services.config_path import live_config_path
from api.services.secrets import get_secret

logger = logging.getLogger(__name__)

# Cache: list of model dicts + expiry timestamp
_cache: Dict[str, Any] = {"models": None, "expires": 0.0}
_cache_lock = asyncio.Lock()
CACHE_TTL_SECONDS = 3600  # 1 hour


def _load_config() -> dict:
    # The live config, not the packaged default, so `available_models` and
    # `model_families` edits made through Settings are seen here too (#440).
    # Looked up per call (no import-time side effects; seeding is startup's job).
    with open(live_config_path()) as f:
        return json.load(f)


def _get_family_patterns(config: dict) -> List[dict]:
    """Return model_families from config, or empty list if not configured."""
    return config.get("model_families", [])


def _match_model(model_id: str, families: List[dict]) -> Optional[dict]:
    """Match a model ID against family patterns, return first match or None."""
    for family in families:
        for pattern in family["patterns"]:
            if fnmatch(model_id, pattern):
                return family
    return None


async def fetch_openrouter_models() -> Optional[List[dict]]:
    """Fetch model list from OpenRouter API.

    Returns the raw model list on success, None on failure.
    """
    api_key = get_secret("OPENROUTER_API_KEY")
    if not api_key:
        logger.warning("OPENROUTER_API_KEY not available, cannot fetch models")
        return None

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://openrouter.ai/api/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", [])
    except (httpx.HTTPError, KeyError, json.JSONDecodeError) as e:
        logger.warning("Failed to fetch OpenRouter models: %s", e)
        return None


# Raw catalog cache for per-model capability lookups (reasoning). Kept apart from
# the classified-roster cache above: that one holds only family-matched models.
_catalog_cache: Dict[str, Any] = {"data": None, "expires": 0.0}

# OpenRouter reasoning efforts, weakest first. "none" is not here on purpose: it
# is the off switch, which mandatory-reasoning models don't offer (#465).
REASONING_EFFORT_ORDER = ("minimal", "low", "medium", "high", "xhigh", "max")


async def get_catalog() -> Optional[List[dict]]:
    """The raw OpenRouter catalog, cached for CACHE_TTL_SECONDS.

    A failed refresh returns the last good copy -- a stale capability answer
    beats none -- so this is None only if no fetch has ever succeeded.
    """
    now = time.time()
    if _catalog_cache["data"] is not None and now < _catalog_cache["expires"]:
        return _catalog_cache["data"]
    raw = await fetch_openrouter_models()
    if raw:
        _catalog_cache["data"] = raw
        _catalog_cache["expires"] = now + CACHE_TTL_SECONDS
        return raw
    return _catalog_cache["data"]


async def reasoning_caps(model_id: Optional[str]) -> Optional[dict]:
    """The catalog's ``reasoning`` block for ``model_id``, e.g.
    ``{"mandatory": True, "supported_efforts": ["max", ..., "low"]}``.

    ``{}`` when the model is listed without one; None when the model or the
    catalog is unknown -- callers then keep their default behaviour.
    """
    if not model_id:
        return None
    raw = await get_catalog()
    if not raw:
        return None
    for m in raw:
        if m.get("id") == model_id:
            return m.get("reasoning") or {}
    return None


def lowest_reasoning_effort(caps: Optional[dict]) -> Optional[str]:
    """Weakest effort the model supports, or None if it lists none we know."""
    efforts = (caps or {}).get("supported_efforts") or []
    for effort in REASONING_EFFORT_ORDER:
        if effort in efforts:
            return effort
    return None


def _classify_models(raw_models: List[dict], families: List[dict]) -> List[dict]:
    """Filter and classify raw OpenRouter models using family patterns.

    Returns a list of dicts matching the AvailableModel schema:
    {id, name, provider, tier}
    """
    results = []
    seen_ids = set()

    for model in raw_models:
        model_id = model.get("id", "")
        if model_id in seen_ids:
            continue

        family = _match_model(model_id, families)
        if family is None:
            continue

        # Extract pricing (OpenRouter reports cost per token as strings)
        pricing_input = None
        pricing_output = None
        raw_pricing = model.get("pricing", {})
        if raw_pricing:
            try:
                prompt_per_token = float(raw_pricing.get("prompt", 0))
                completion_per_token = float(raw_pricing.get("completion", 0))
                # Convert per-token to per-1M-tokens
                pricing_input = round(prompt_per_token * 1_000_000, 4)
                pricing_output = round(completion_per_token * 1_000_000, 4)
            except (ValueError, TypeError):
                pass

        seen_ids.add(model_id)
        results.append(
            {
                "id": model_id,
                "name": model.get("name", model_id),
                "provider": family["provider"],
                "tier": family["tier"],
                "pricing_input": pricing_input,
                "pricing_output": pricing_output,
            }
        )

    # Sort by tier, then name
    results.sort(key=lambda m: (m["tier"], m["name"]))
    return results


def _static_fallback(config: dict) -> List[dict]:
    """Return the static available_models from config as fallback."""
    return config.get("available_models", [])


# Prettified labels for common OpenAI-compatible servers' ``owned_by`` value.
# Cosmetic only (per-server brand, not per-model); unknown values pass through.
_PROVIDER_LABELS = {
    "omlx": "oMLX",
    "vllm": "vLLM",
    "llamacpp": "llama.cpp",
    "llama-cpp": "llama.cpp",
    "lmstudio": "LM Studio",
    "ollama": "Ollama",
}


def _provider_label(owned_by: Optional[str]) -> str:
    """Human label for a discovered model's serving software."""
    if not owned_by:
        return "Local"
    return _PROVIDER_LABELS.get(owned_by.lower(), owned_by)


def _resolve_backend_endpoint(cfg: dict) -> str:
    """A backend's endpoint, honoring an ``endpoint_env`` override."""
    env = cfg.get("endpoint_env")
    return os.getenv(env, cfg["endpoint"]) if env else cfg["endpoint"]


def _models_url(endpoint: str) -> str:
    """Derive the ``/v1/models`` URL from a chat endpoint, a ``/v1`` base, or an
    endpoint that already points at ``/models``."""
    stripped = endpoint.rstrip("/")
    if stripped.endswith("/chat/completions"):
        stripped = stripped[: -len("/chat/completions")]
    stripped = stripped.rstrip("/")
    # Already a models endpoint (e.g. ".../v1/models") — don't append a second
    # "/models" and produce ".../models/models".
    if stripped.endswith("/models"):
        return stripped
    return stripped + "/models"


async def fetch_local_models(config: dict) -> List[dict]:
    """Discover models from each enabled, ``discover``-flagged local (openai-type)
    backend via its ``/v1/models`` endpoint.

    Returns roster entries merged **unfiltered** (no family-pattern filtering, so a
    brand-new model is never dropped), each tagged by serving software (``provider``
    from the server's ``owned_by``) and ``host``, at $0. ``backend`` is the config
    key that routes to it. A backend that errors contributes nothing (logged) —
    discovery is never fatal.
    """
    results: List[dict] = []
    for name, cfg in config.get("backends", {}).items():
        if not (cfg.get("enabled") and cfg.get("discover") and cfg.get("type") == "openai"):
            continue
        endpoint = _resolve_backend_endpoint(cfg)
        url = _models_url(endpoint)
        host = urlparse(endpoint).netloc or endpoint
        api_key = get_secret(cfg["api_key_env"]) if cfg.get("api_key_env") else None
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json().get("data", [])
        except (httpx.HTTPError, KeyError, json.JSONDecodeError) as e:
            logger.warning("Local model discovery failed for %s (%s): %s", name, url, e)
            continue
        for m in data:
            model_id = m.get("id")
            if not model_id:
                continue
            results.append(
                {
                    "id": model_id,
                    "name": m.get("name") or model_id,
                    "provider": _provider_label(m.get("owned_by")),
                    "backend": name,
                    "host": host,
                    "tier": None,
                    "pricing_input": 0,
                    "pricing_output": 0,
                    "context_len": m.get("max_model_len"),
                }
            )
    return results


async def get_available_models() -> List[dict]:
    """Get the current model roster, using cache when fresh.

    Priority:
    1. Cached dynamic roster (if within TTL)
    2. Fresh fetch from OpenRouter → classify → cache
    3. Static fallback from config
    """
    now = time.time()

    # Fast path: return cache if fresh
    if _cache["models"] is not None and now < _cache["expires"]:
        return _cache["models"]

    async with _cache_lock:
        # Re-check after acquiring lock (another coroutine may have refreshed)
        now = time.time()
        if _cache["models"] is not None and now < _cache["expires"]:
            return _cache["models"]

        config = _load_config()
        families = _get_family_patterns(config)

        # Locally-discovered models are always merged into whatever cloud roster is
        # built below. They ride along with it rather than being cached separately.
        local = await fetch_local_models(config)

        # If no families configured, use static cloud list (uncached, so a later
        # config change is picked up next call) + local.
        if not families:
            logger.info("No model_families configured, using static available_models")
            return _static_fallback(config) + local

        raw_models = await fetch_openrouter_models()
        if raw_models is None:
            logger.info("OpenRouter fetch failed, using static fallback")
            return _static_fallback(config) + local

        classified = _classify_models(raw_models, families)
        if not classified:
            logger.warning("No models matched family patterns, using static fallback")
            return _static_fallback(config) + local

        # Cache only when the cloud roster came from a successful dynamic fetch.
        models = classified + local
        _cache["models"] = models
        _cache["expires"] = now + CACHE_TTL_SECONDS
        logger.info("Refreshed model roster: %d cloud + %d local", len(classified), len(local))
        return models


def invalidate_cache() -> None:
    """Clear the model roster cache, forcing a refresh on next call."""
    _cache["models"] = None
    _cache["expires"] = 0.0
    _catalog_cache["data"] = None
    _catalog_cache["expires"] = 0.0


async def newest_in_family(family: str, exclude_variants: list) -> Optional[str]:
    """Newest anthropic/* model id in `family` by OpenRouter `created`, excluding
    any id containing an excluded variant token (e.g. 'fast', 'fable') and any
    ``:``-suffixed variant. Every model has a ``:batch`` twin with the same
    ``created`` stamp, so without that a tie could resolve to the batch endpoint.

    Returns None on fetch failure or no match — callers fall through to
    pause-and-suggest rather than guessing.
    """
    raw = await fetch_openrouter_models()
    if not raw:
        return None
    family = family.lower()
    candidates = []
    for m in raw:
        mid = (m.get("id") or "").lower()
        if not mid.startswith("anthropic/") or family not in mid or ":" in mid:
            continue
        if any(v.lower() in mid for v in exclude_variants):
            continue
        candidates.append((m.get("created") or 0, m["id"]))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates[0][1]
