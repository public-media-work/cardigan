from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.services import model_roster


def _discover_config():
    return {
        "backends": {
            "local-llm": {
                "type": "openai",
                "endpoint": "http://studio.riechers.co:8000/v1/chat/completions",
                "api_key_env": "LOCAL_LLM_API_KEY",
                "enabled": True,
                "discover": True,
            },
            # openrouter is not an openai/discover backend -> must be skipped
            "openrouter": {"type": "openrouter", "enabled": True},
            # a disabled local backend -> must be skipped
            "local-off": {"type": "openai", "endpoint": "http://x/v1", "enabled": False, "discover": True},
        }
    }


def _mock_models_response(payload):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = payload
    return resp


def test_models_url_derivation_is_idempotent():
    f = model_roster._models_url
    # chat endpoint -> /v1/models
    assert f("http://h:8000/v1/chat/completions") == "http://h:8000/v1/models"
    # /v1 base -> /v1/models
    assert f("http://h:8000/v1") == "http://h:8000/v1/models"
    # trailing slashes tolerated
    assert f("http://h:8000/v1/") == "http://h:8000/v1/models"
    # already a models endpoint -> unchanged (no ".../models/models")
    assert f("http://h:8000/v1/models") == "http://h:8000/v1/models"
    assert f("http://h:8000/v1/models/") == "http://h:8000/v1/models"


@pytest.mark.asyncio
async def test_fetch_local_models_tags_by_server_and_host_unfiltered():
    payload = {
        "data": [
            {"id": "Qwen2.5-7B-Instruct-4bit", "owned_by": "omlx", "max_model_len": 32768},
            {"id": "some-unheard-of-model", "owned_by": "omlx", "max_model_len": 4096},
        ]
    }
    get_mock = AsyncMock(return_value=_mock_models_response(payload))
    with patch.object(model_roster.httpx.AsyncClient, "get", get_mock):
        with patch.object(model_roster, "get_secret", return_value="k"):
            got = await model_roster.fetch_local_models(_discover_config())

    # Discovery derives the /v1/models URL from the chat endpoint and sends the key.
    assert get_mock.call_args.args[0] == "http://studio.riechers.co:8000/v1/models"
    assert get_mock.call_args.kwargs["headers"]["Authorization"] == "Bearer k"
    # Every served model is kept — no family-pattern filtering.
    assert [m["id"] for m in got] == ["Qwen2.5-7B-Instruct-4bit", "some-unheard-of-model"]
    first = got[0]
    assert first["provider"] == "oMLX"  # from owned_by, prettified
    assert first["backend"] == "local-llm"  # routing key
    assert first["host"] == "studio.riechers.co:8000"
    assert first["pricing_input"] == 0 and first["pricing_output"] == 0
    assert first["context_len"] == 32768
    assert first["tier"] is None


@pytest.mark.asyncio
async def test_fetch_local_models_non_fatal_on_unreachable():
    import httpx

    with patch.object(model_roster.httpx.AsyncClient, "get", AsyncMock(side_effect=httpx.ConnectError("down"))):
        with patch.object(model_roster, "get_secret", return_value="k"):
            got = await model_roster.fetch_local_models(_discover_config())
    assert got == []  # a down endpoint contributes nothing, no raise


@pytest.mark.asyncio
async def test_get_available_models_merges_cloud_and_local():
    model_roster.invalidate_cache()
    config = {
        "model_families": [{"name": "Claude", "provider": "Anthropic", "tier": 0, "patterns": ["anthropic/*"]}],
        "backends": _discover_config()["backends"],
        "available_models": [],
    }
    cloud_raw = [
        {"id": "anthropic/claude-haiku-4.5", "name": "Claude Haiku", "pricing": {"prompt": "0", "completion": "0"}}
    ]
    local = [
        {
            "id": "Qwen2.5-7B-Instruct-4bit",
            "provider": "oMLX",
            "backend": "local-llm",
            "host": "studio.riechers.co:8000",
            "tier": None,
            "pricing_input": 0,
            "pricing_output": 0,
            "context_len": 32768,
        }
    ]

    with patch.object(model_roster, "_load_config", return_value=config):
        with patch.object(model_roster, "fetch_openrouter_models", AsyncMock(return_value=cloud_raw)):
            with patch.object(model_roster, "fetch_local_models", AsyncMock(return_value=local)):
                got = await model_roster.get_available_models()

    model_roster.invalidate_cache()
    ids = [m["id"] for m in got]
    assert "anthropic/claude-haiku-4.5" in ids  # cloud roster
    assert "Qwen2.5-7B-Instruct-4bit" in ids  # local merged in alongside


@pytest.mark.asyncio
async def test_newest_in_family_picks_newest_excludes_variants():
    raw = [
        {"id": "anthropic/claude-opus-4-6", "created": 100},
        {"id": "anthropic/claude-opus-4-8", "created": 300},  # newest opus
        {"id": "anthropic/claude-opus-4-8-fast", "created": 400},  # excluded (fast)
        {"id": "anthropic/claude-fable-5", "created": 500},  # excluded (fable)
        {"id": "anthropic/claude-sonnet-4-6", "created": 200},
        {"id": "openai/gpt-4o", "created": 999},  # wrong provider
    ]
    with patch.object(model_roster, "fetch_openrouter_models", AsyncMock(return_value=raw)):
        got = await model_roster.newest_in_family("opus", ["fast", "fable"])
    assert got == "anthropic/claude-opus-4-8"


@pytest.mark.asyncio
async def test_newest_in_family_none_on_fetch_failure():
    with patch.object(model_roster, "fetch_openrouter_models", AsyncMock(return_value=None)):
        assert await model_roster.newest_in_family("opus", ["fast"]) is None


@pytest.mark.asyncio
async def test_newest_in_family_skips_batch_twin():
    """Every model has a ``:batch`` twin with the same ``created`` stamp (#465)."""
    raw = [
        {"id": "anthropic/claude-sonnet-5.5:batch", "created": 300},
        {"id": "anthropic/claude-sonnet-5.5", "created": 300},
        {"id": "anthropic/claude-sonnet-4.6", "created": 200},
    ]
    with patch.object(model_roster, "fetch_openrouter_models", AsyncMock(return_value=raw)):
        assert await model_roster.newest_in_family("sonnet", ["fast", "fable"]) == "anthropic/claude-sonnet-5.5"


CATALOG = [
    {
        "id": "anthropic/claude-sonnet-5.5",
        "reasoning": {"mandatory": True, "supported_efforts": ["max", "xhigh", "high", "medium", "low"]},
    },
    {
        "id": "anthropic/claude-sonnet-4.6",
        "reasoning": {"mandatory": False, "supported_efforts": ["max", "high", "medium", "low", "none"]},
    },
    {"id": "anthropic/claude-haiku-4.5"},
]


@pytest.fixture
def fresh_catalog_cache():
    model_roster.invalidate_cache()
    yield
    model_roster.invalidate_cache()


@pytest.mark.asyncio
async def test_reasoning_caps_reads_catalog(fresh_catalog_cache):
    with patch.object(model_roster, "fetch_openrouter_models", AsyncMock(return_value=CATALOG)):
        caps = await model_roster.reasoning_caps("anthropic/claude-sonnet-5.5")
        assert caps["mandatory"] is True
        assert await model_roster.reasoning_caps("anthropic/claude-haiku-4.5") == {}
        assert await model_roster.reasoning_caps("anthropic/not-listed") is None
        assert await model_roster.reasoning_caps(None) is None


@pytest.mark.asyncio
async def test_catalog_is_cached_and_survives_a_failed_refresh(fresh_catalog_cache):
    fetch = AsyncMock(return_value=CATALOG)
    with patch.object(model_roster, "fetch_openrouter_models", fetch):
        await model_roster.reasoning_caps("anthropic/claude-sonnet-5.5")
        await model_roster.reasoning_caps("anthropic/claude-sonnet-4.6")
    assert fetch.await_count == 1

    model_roster._catalog_cache["expires"] = 0.0  # force a refresh, which fails
    with patch.object(model_roster, "fetch_openrouter_models", AsyncMock(return_value=None)):
        caps = await model_roster.reasoning_caps("anthropic/claude-sonnet-5.5")
    assert caps["mandatory"] is True


@pytest.mark.asyncio
async def test_reasoning_caps_none_without_catalog(fresh_catalog_cache):
    with patch.object(model_roster, "fetch_openrouter_models", AsyncMock(return_value=None)):
        assert await model_roster.reasoning_caps("anthropic/claude-sonnet-5.5") is None


def test_lowest_reasoning_effort_skips_none():
    assert model_roster.lowest_reasoning_effort(CATALOG[0]["reasoning"]) == "low"
    assert model_roster.lowest_reasoning_effort(CATALOG[1]["reasoning"]) == "low"
    assert model_roster.lowest_reasoning_effort({"supported_efforts": ["high", "minimal"]}) == "minimal"
    assert model_roster.lowest_reasoning_effort({}) is None
    assert model_roster.lowest_reasoning_effort(None) is None
