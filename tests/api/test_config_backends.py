"""Tests for the backend-definition CRUD API (/api/config/backends).

Step 3 of the self-updating local-model backends design: a user adds a local
OpenAI-compatible endpoint and it becomes a discoverable, routable backend —
keyed by host so it self-identifies, with no code or config-file hand-edit.
"""

import json

import pytest


@pytest.fixture
def cfg_path(monkeypatch, tmp_path):
    """A throwaway config the CRUD endpoints read/write, so tests never touch
    the committed config/llm-config.json."""
    cfg = {
        "primary_backend": "openrouter",
        "backends": {
            "openrouter": {
                "type": "openrouter",
                "endpoint": "https://openrouter.ai/api/v1/chat/completions",
            }
        },
        "phase_backends": {"analyst": "openrouter"},
        "phase_models": {},
    }
    p = tmp_path / "llm-config.json"
    p.write_text(json.dumps(cfg))
    monkeypatch.setattr("api.routers.config.CONFIG_PATH", p)
    return p


@pytest.fixture(autouse=True)
def no_live_alias_lookup(monkeypatch):
    """GET/PATCH /config/models resolve "latest" aliases through the catalog;
    keep every test here off the network (#499)."""

    async def _unresolved(alias):
        return None

    monkeypatch.setattr("api.routers.config.model_roster.resolve_alias", _unresolved)


def _backends(cfg_path):
    return json.loads(cfg_path.read_text())["backends"]


def test_create_backend_is_host_keyed_with_defaults(api_client, cfg_path):
    resp = api_client.post(
        "/api/config/backends",
        json={"endpoint": "http://studio.riechers.co:8000/v1", "api_key_env": "LOCAL_LLM_API_KEY"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == "studio.riechers.co:8000"  # host-keyed, self-identifying

    entry = _backends(cfg_path)["studio.riechers.co:8000"]
    assert entry["type"] == "openai"
    assert entry["endpoint"] == "http://studio.riechers.co:8000/v1"
    assert entry["enabled"] is True
    assert entry["discover"] is True
    assert entry["cost_per_project"] == 0.0
    assert entry["api_key_env"] == "LOCAL_LLM_API_KEY"


def test_create_backend_rejects_duplicate_host(api_client, cfg_path):
    body = {"endpoint": "http://studio.riechers.co:8000/v1"}
    assert api_client.post("/api/config/backends", json=body).status_code == 201
    assert api_client.post("/api/config/backends", json=body).status_code == 409


def test_create_backend_rejects_invalid_endpoint(api_client, cfg_path):
    resp = api_client.post("/api/config/backends", json={"endpoint": "not-a-url"})
    assert resp.status_code == 400


def test_list_backends_includes_created(api_client, cfg_path):
    api_client.post("/api/config/backends", json={"endpoint": "http://studio.riechers.co:8000/v1"})
    resp = api_client.get("/api/config/backends")
    assert resp.status_code == 200
    names = {b["name"] for b in resp.json()["backends"]}
    assert {"openrouter", "studio.riechers.co:8000"} <= names


def test_patch_backend_toggles_flags(api_client, cfg_path):
    api_client.post("/api/config/backends", json={"endpoint": "http://studio.riechers.co:8000/v1"})
    resp = api_client.patch(
        "/api/config/backends/studio.riechers.co:8000",
        json={"enabled": False, "discover": False},
    )
    assert resp.status_code == 200, resp.text
    entry = _backends(cfg_path)["studio.riechers.co:8000"]
    assert entry["enabled"] is False and entry["discover"] is False


def test_patch_unknown_backend_404(api_client, cfg_path):
    assert api_client.patch("/api/config/backends/nope:1", json={"enabled": False}).status_code == 404


def test_patch_backend_rejects_invalid_endpoint(api_client, cfg_path):
    # update_backend must apply create_backend's absolute-URL guard, so a PATCH
    # can't repoint a backend at a relative/garbage endpoint.
    api_client.post("/api/config/backends", json={"endpoint": "http://studio.riechers.co:8000/v1"})
    resp = api_client.patch("/api/config/backends/studio.riechers.co:8000", json={"endpoint": "not-a-url"})
    assert resp.status_code == 400, resp.text
    # The bad value must not have been written.
    assert _backends(cfg_path)["studio.riechers.co:8000"]["endpoint"] == "http://studio.riechers.co:8000/v1"


def test_delete_backend(api_client, cfg_path):
    api_client.post("/api/config/backends", json={"endpoint": "http://studio.riechers.co:8000/v1"})
    resp = api_client.delete("/api/config/backends/studio.riechers.co:8000")
    assert resp.status_code == 204
    assert "studio.riechers.co:8000" not in _backends(cfg_path)


def test_delete_referenced_backend_conflicts(api_client, cfg_path):
    # openrouter is primary_backend AND used by phase_backends -> must not orphan routing
    assert api_client.delete("/api/config/backends/openrouter").status_code == 409


def test_delete_backend_referenced_only_by_fallback_conflicts(api_client, monkeypatch, tmp_path):
    """A backend used *solely* as fallback_backend (not primary, not in any phase)
    must still refuse deletion, so the fallback chain can't be orphaned."""
    cfg = {
        "primary_backend": "openrouter",
        "fallback_backend": "studio.riechers.co:8000",
        "backends": {
            "openrouter": {"type": "openrouter", "endpoint": "https://openrouter.ai/api/v1/chat/completions"},
            "studio.riechers.co:8000": {
                "type": "openai",
                "endpoint": "http://studio.riechers.co:8000/v1",
                "enabled": True,
                "discover": True,
            },
        },
        "phase_backends": {"analyst": "openrouter"},
        "phase_models": {},
    }
    p = tmp_path / "fallback.json"
    p.write_text(json.dumps(cfg))
    monkeypatch.setattr("api.routers.config.CONFIG_PATH", p)

    resp = api_client.delete("/api/config/backends/studio.riechers.co:8000")
    assert resp.status_code == 409, resp.text
    assert "fallback_backend" in resp.json()["detail"]


def test_get_models_includes_local_models_with_host_and_backend(api_client, cfg_path, monkeypatch):
    """GET /config/models must not choke on a local model (tier=None) and must
    pass through host/backend so the dropdown can group by server."""
    roster = [
        {
            "id": "anthropic/claude-haiku-4.5",
            "name": "Claude Haiku 4.5",
            "provider": "Anthropic",
            "tier": 0,
            "pricing_input": 0.8,
            "pricing_output": 4.0,
        },
        {
            "id": "Qwen2.5-7B-Instruct-4bit",
            "name": "Qwen2.5-7B-Instruct-4bit",
            "provider": "oMLX",
            "backend": "studio.riechers.co:8000",
            "host": "studio.riechers.co:8000",
            "tier": None,
            "pricing_input": 0,
            "pricing_output": 0,
            "context_len": 32768,
        },
    ]

    async def _roster():
        return roster

    monkeypatch.setattr("api.routers.config.get_available_models", _roster)

    resp = api_client.get("/api/config/models")
    assert resp.status_code == 200, resp.text
    by_id = {m["id"]: m for m in resp.json()["available_models"]}
    local = by_id["Qwen2.5-7B-Instruct-4bit"]
    assert local["provider"] == "oMLX"
    assert local["host"] == "studio.riechers.co:8000"
    assert local["backend"] == "studio.riechers.co:8000"
    assert local["tier"] is None


def _setup_pair(monkeypatch, tmp_path, seo_backend):
    """Install a config with a local backend + a patched roster, with the SEO
    phase initially routed to `seo_backend`."""
    cfg = {
        "primary_backend": "openrouter",
        "backends": {
            "openrouter": {"type": "openrouter", "endpoint": "https://openrouter.ai/api/v1/chat/completions"},
            "openrouter-cheapskate": {
                "type": "openrouter",
                "endpoint": "https://openrouter.ai/api/v1/chat/completions",
            },
            "studio.riechers.co:8000": {
                "type": "openai",
                "endpoint": "http://studio.riechers.co:8000/v1",
                "enabled": True,
                "discover": True,
            },
        },
        "phase_backends": {"seo": seo_backend},
        "phase_models": {},
    }
    p = tmp_path / "pair.json"
    p.write_text(json.dumps(cfg))
    monkeypatch.setattr("api.routers.config.CONFIG_PATH", p)

    roster = [
        {
            "id": "Qwen2.5-7B-Instruct-4bit",
            "name": "Qwen2.5-7B-Instruct-4bit",
            "provider": "oMLX",
            "backend": "studio.riechers.co:8000",
            "host": "studio.riechers.co:8000",
            "tier": None,
        },
        {"id": "anthropic/claude-haiku-4.5", "name": "Claude Haiku 4.5", "provider": "Anthropic", "tier": 0},
    ]

    async def _roster():
        return roster

    monkeypatch.setattr("api.routers.config.get_available_models", _roster)
    return p


def test_assigning_local_model_writes_phase_backend(api_client, monkeypatch, tmp_path):
    p = _setup_pair(monkeypatch, tmp_path, "openrouter")
    resp = api_client.patch("/api/config/models", json={"phase_models": {"seo": "Qwen2.5-7B-Instruct-4bit"}})
    assert resp.status_code == 200, resp.text
    saved = json.loads(p.read_text())
    assert saved["phase_models"]["seo"] == "Qwen2.5-7B-Instruct-4bit"
    assert saved["phase_backends"]["seo"] == "studio.riechers.co:8000"  # pair written


def test_assigning_cloud_model_resets_off_local_backend(api_client, monkeypatch, tmp_path):
    p = _setup_pair(monkeypatch, tmp_path, "studio.riechers.co:8000")  # phase currently local
    resp = api_client.patch("/api/config/models", json={"phase_models": {"seo": "anthropic/claude-haiku-4.5"}})
    assert resp.status_code == 200, resp.text
    saved = json.loads(p.read_text())
    assert saved["phase_backends"]["seo"] == "openrouter"  # reset to primary cloud backend


def test_assigning_cloud_model_keeps_existing_cloud_backend(api_client, monkeypatch, tmp_path):
    p = _setup_pair(monkeypatch, tmp_path, "openrouter-cheapskate")  # already a cloud tier
    resp = api_client.patch("/api/config/models", json={"phase_models": {"seo": "anthropic/claude-haiku-4.5"}})
    assert resp.status_code == 200, resp.text
    saved = json.loads(p.read_text())
    assert saved["phase_backends"]["seo"] == "openrouter-cheapskate"  # unchanged


def test_assigning_model_with_empty_roster_is_rejected(api_client, monkeypatch, tmp_path):
    """If the roster is unavailable (e.g. OpenRouter unreachable, cold cache), the
    (backend, model) pair can't be resolved — assignment must 503 rather than
    silently save a model_id with a stale phase_backends entry."""
    p = _setup_pair(monkeypatch, tmp_path, "openrouter")

    async def _empty_roster():
        return []

    monkeypatch.setattr("api.routers.config.get_available_models", _empty_roster)

    resp = api_client.patch("/api/config/models", json={"phase_models": {"seo": "Qwen2.5-7B-Instruct-4bit"}})
    assert resp.status_code == 503, resp.text
    # config must be untouched — no half-written pairing
    saved = json.loads(p.read_text())
    assert saved["phase_models"] == {}
    assert saved["phase_backends"]["seo"] == "openrouter"


SONNET_ALIAS = "~anthropic/claude-sonnet-latest"


def _resolve_known(monkeypatch, known):
    async def _resolve(alias):
        return known.get(alias)

    monkeypatch.setattr("api.routers.config.model_roster.resolve_alias", _resolve)


def test_get_models_lists_latest_aliases_with_current_target(api_client, monkeypatch, tmp_path):
    _setup_pair(monkeypatch, tmp_path, "openrouter")
    _resolve_known(monkeypatch, {SONNET_ALIAS: "anthropic/claude-sonnet-5.5"})

    resp = api_client.get("/api/config/models")
    assert resp.status_code == 200, resp.text
    aliases = {a["family"]: a for a in resp.json()["model_aliases"]}
    assert list(aliases) == ["haiku", "sonnet", "opus"]
    assert aliases["sonnet"] == {
        "id": SONNET_ALIAS,
        "name": "Latest Claude Sonnet",
        "family": "sonnet",
        "resolves_to": "anthropic/claude-sonnet-5.5",
    }
    assert aliases["opus"]["resolves_to"] is None  # unknown, still offered


def test_assigning_alias_saves_the_alias_not_its_target(api_client, monkeypatch, tmp_path):
    """phase_models keeps the alias so the phase follows new releases (#499)."""
    p = _setup_pair(monkeypatch, tmp_path, "studio.riechers.co:8000")  # phase currently local
    _resolve_known(monkeypatch, {SONNET_ALIAS: "anthropic/claude-sonnet-5.5"})

    resp = api_client.patch("/api/config/models", json={"phase_models": {"seo": SONNET_ALIAS}})
    assert resp.status_code == 200, resp.text
    saved = json.loads(p.read_text())
    assert saved["phase_models"]["seo"] == SONNET_ALIAS
    assert saved["phase_backends"]["seo"] == "openrouter"  # an alias is cloud: reset off local


def test_assigning_unresolvable_alias_is_rejected(api_client, monkeypatch, tmp_path):
    p = _setup_pair(monkeypatch, tmp_path, "openrouter")

    resp = api_client.patch("/api/config/models", json={"phase_models": {"seo": "~anthropic/claude-nope-latest"}})
    assert resp.status_code == 400, resp.text
    assert "alias" in resp.json()["detail"]
    assert json.loads(p.read_text())["phase_models"] == {}
