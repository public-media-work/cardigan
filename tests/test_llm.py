"""Tests for LLM service layer — credit-exhaustion error detection."""

from unittest.mock import AsyncMock

import pytest

from api.services.llm import CreditExhaustedError, LLMClient


class FakeResp402:
    """Minimal fake httpx response for a 402 Payment Required (credit exhausted)."""

    status_code = 402
    text = '{"error":{"message":"Insufficient credits"}}'

    def json(self):
        return {"error": {"message": "Insufficient credits"}}


class FakeRespCreditBodyExhausted:
    """Minimal fake httpx response for a non-402 4xx with credit-exhaustion body keywords."""

    status_code = 400
    text = '{"error":{"message":"Your credit balance is exhausted"}}'

    def json(self):
        return {"error": {"message": "Your credit balance is exhausted"}}


async def test_call_openrouter_raises_credit_exhausted_on_402(monkeypatch):
    """_call_openrouter raises CreditExhaustedError on HTTP 402 before raise_for_status."""
    client = LLMClient.__new__(LLMClient)
    client.active_backend = "openrouter"

    # _post_openrouter is the seam isolating the HTTP boundary; patch it to 402.
    monkeypatch.setattr(client, "_post_openrouter", AsyncMock(return_value=FakeResp402()))

    with pytest.raises(CreditExhaustedError):
        await client._call_openrouter(
            config={"endpoint": "https://openrouter.ai/api/v1/chat/completions"},
            model="anthropic/x",
            messages=[],
            api_key="fake-key",
        )


async def test_call_openrouter_raises_credit_exhausted_on_credit_body(monkeypatch):
    """_call_openrouter raises CreditExhaustedError on non-402 4xx with credit-body keywords.

    Tests the body-keyword detection path: when a non-402 status arrives with
    an error body containing "credit" + "balance" (or "exhaust"/"quota").
    """
    client = LLMClient.__new__(LLMClient)
    client.active_backend = "openrouter"

    # Patch _post_openrouter with a 400 (not 402) so only the body path triggers.
    monkeypatch.setattr(client, "_post_openrouter", AsyncMock(return_value=FakeRespCreditBodyExhausted()))

    with pytest.raises(CreditExhaustedError):
        await client._call_openrouter(
            config={"endpoint": "https://openrouter.ai/api/v1/chat/completions"},
            model="anthropic/x",
            messages=[],
            api_key="fake-key",
        )


class FakeRespOK:
    """Minimal fake httpx response for a successful OpenRouter completion.

    ``finish_reason`` is parameterized so tests can distinguish a natural stop
    from a provider-side length cutoff.
    """

    status_code = 200

    def __init__(self, finish_reason="stop", content="hello"):
        self._finish_reason = finish_reason
        self._content = content
        self.text = "{}"

    def json(self):
        return {
            "model": "anthropic/claude-sonnet-4.6",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "choices": [
                {
                    "message": {"content": self._content},
                    "finish_reason": self._finish_reason,
                }
            ],
        }

    def raise_for_status(self):
        return None


def _capturing_post(captured: dict):
    """Patch seam for _post_openrouter that records the outgoing payload."""

    async def _post(endpoint, headers, payload):
        captured["payload"] = payload
        return FakeRespOK()

    return _post


async def test_call_openrouter_sends_explicit_max_tokens(monkeypatch):
    """OpenRouter payload must carry an explicit max_tokens (#403).

    Without it the provider applies its own undocumented completion cap, which
    silently truncated the formatter on job 48.
    """
    client = LLMClient.__new__(LLMClient)
    client.active_backend = "openrouter"

    captured = {}
    monkeypatch.setattr(client, "_post_openrouter", _capturing_post(captured))

    await client._call_openrouter(
        config={"endpoint": "https://openrouter.ai/api/v1/chat/completions"},
        model="anthropic/claude-sonnet-4.6",
        messages=[{"role": "user", "content": "hi"}],
        api_key="fake-key",
    )

    assert "max_tokens" in captured["payload"], "OpenRouter payload must set max_tokens explicitly"
    assert captured["payload"]["max_tokens"] == 4096


async def test_call_openrouter_max_tokens_honors_backend_config(monkeypatch):
    """Backend config max_tokens overrides the built-in default (#403)."""
    client = LLMClient.__new__(LLMClient)
    client.active_backend = "openrouter"

    captured = {}
    monkeypatch.setattr(client, "_post_openrouter", _capturing_post(captured))

    await client._call_openrouter(
        config={"endpoint": "https://openrouter.ai/api/v1/chat/completions", "max_tokens": 16384},
        model="anthropic/claude-sonnet-4.6",
        messages=[{"role": "user", "content": "hi"}],
        api_key="fake-key",
    )

    assert captured["payload"]["max_tokens"] == 16384


def test_packaged_openrouter_backends_all_set_max_tokens():
    """Every shipped OpenRouter backend sets max_tokens (#470 job 26).

    #403 capped only the ``openrouter`` backend, so the analyst phase on
    ``openrouter-cheapskate`` hit the 4096 default on an 18-minute transcript.
    """
    import json
    from pathlib import Path

    config = json.loads((Path(__file__).resolve().parents[1] / "config" / "llm-config.json").read_text())
    uncapped = [
        name
        for name, backend in config["backends"].items()
        if backend.get("type") == "openrouter" and not backend.get("max_tokens")
    ]
    assert not uncapped, f"OpenRouter backends without max_tokens fall back to 4096: {uncapped}"


MANDATORY_CAPS = {"mandatory": True, "supported_efforts": ["max", "xhigh", "high", "medium", "low"]}
OPTIONAL_CAPS = {"mandatory": False, "supported_efforts": ["max", "high", "medium", "low", "none"]}


def _openrouter_client():
    client = LLMClient.__new__(LLMClient)
    client.active_backend = "openrouter"
    client.config = {}
    return client


async def _send_reasoning_off(client, model="anthropic/claude-sonnet-5.5"):
    return await client._call_openrouter(
        config={"endpoint": "https://openrouter.ai/api/v1/chat/completions"},
        model=model,
        messages=[{"role": "user", "content": "hi"}],
        api_key="fake-key",
        reasoning={"enabled": False},
    )


async def test_reasoning_off_becomes_lowest_effort_on_mandatory_model(monkeypatch):
    """Sonnet 5.5 rejects ``enabled: false`` with HTTP 400; send its weakest effort (#465)."""
    from api.services import model_roster

    monkeypatch.setattr(model_roster, "reasoning_caps", AsyncMock(return_value=MANDATORY_CAPS))
    client = _openrouter_client()
    captured = {}
    monkeypatch.setattr(client, "_post_openrouter", _capturing_post(captured))

    await _send_reasoning_off(client)

    assert captured["payload"]["reasoning"] == {"effort": "low"}


async def test_reasoning_off_kept_on_optional_model(monkeypatch):
    from api.services import model_roster

    monkeypatch.setattr(model_roster, "reasoning_caps", AsyncMock(return_value=OPTIONAL_CAPS))
    client = _openrouter_client()
    captured = {}
    monkeypatch.setattr(client, "_post_openrouter", _capturing_post(captured))

    await _send_reasoning_off(client, model="anthropic/claude-sonnet-4.6")

    assert captured["payload"]["reasoning"] == {"enabled": False}


async def test_reasoning_off_kept_when_catalog_unknown(monkeypatch):
    from api.services import model_roster

    monkeypatch.setattr(model_roster, "reasoning_caps", AsyncMock(return_value=None))
    client = _openrouter_client()
    captured = {}
    monkeypatch.setattr(client, "_post_openrouter", _capturing_post(captured))

    await _send_reasoning_off(client)

    assert captured["payload"]["reasoning"] == {"enabled": False}


class FakeResp400ReasoningMandatory:
    status_code = 400
    text = '{"error":{"message":"Reasoning is mandatory for this endpoint and cannot be disabled.","code":400}}'

    def json(self):
        return {"error": {"message": "Reasoning is mandatory for this endpoint and cannot be disabled.", "code": 400}}

    def raise_for_status(self):
        raise AssertionError("the 400 should have been retried, not raised")


async def test_mandatory_reasoning_400_retries_once_at_low_effort(monkeypatch):
    """Catalog couldn't say, the provider refused reasoning-off: retry at low effort."""
    from api.services import model_roster

    monkeypatch.setattr(model_roster, "reasoning_caps", AsyncMock(return_value=None))
    client = _openrouter_client()
    sent = []

    async def _post(endpoint, headers, payload):
        sent.append(dict(payload))
        return FakeResp400ReasoningMandatory() if len(sent) == 1 else FakeRespOK()

    monkeypatch.setattr(client, "_post_openrouter", _post)

    resp = await _send_reasoning_off(client)

    assert [p["reasoning"] for p in sent] == [{"enabled": False}, {"effort": "low"}]
    assert resp.content == "hello"


async def test_no_reasoning_lookup_without_reasoning_off(monkeypatch):
    """Calls that don't ask for reasoning off never touch the catalog."""
    from api.services import model_roster

    caps = AsyncMock(return_value=MANDATORY_CAPS)
    monkeypatch.setattr(model_roster, "reasoning_caps", caps)
    client = _openrouter_client()
    captured = {}
    monkeypatch.setattr(client, "_post_openrouter", _capturing_post(captured))

    await client._call_openrouter(
        config={"endpoint": "https://openrouter.ai/api/v1/chat/completions"},
        model="anthropic/claude-sonnet-5.5",
        messages=[{"role": "user", "content": "hi"}],
        api_key="fake-key",
    )

    caps.assert_not_awaited()
    assert "reasoning" not in captured["payload"]


SONNET_ALIAS = "~anthropic/claude-sonnet-latest"


async def test_resolve_model_id_passes_pinned_ids_through(monkeypatch):
    from api.services import model_roster

    resolve = AsyncMock()
    monkeypatch.setattr(model_roster, "resolve_alias", resolve)
    client = _openrouter_client()
    assert await client.resolve_model_id("anthropic/claude-sonnet-4.6", job_id=1) == "anthropic/claude-sonnet-4.6"
    assert await client.resolve_model_id(None, job_id=1) is None
    resolve.assert_not_awaited()


async def test_alias_is_pinned_for_the_rest_of_the_job(monkeypatch):
    """The alias moving mid-job must not change the model a job runs on (#499)."""
    from api.services import model_roster

    resolve = AsyncMock(side_effect=["anthropic/claude-sonnet-5.5", "anthropic/claude-sonnet-6"])
    monkeypatch.setattr(model_roster, "resolve_alias", resolve)
    client = _openrouter_client()

    assert await client.resolve_model_id(SONNET_ALIAS, job_id=7) == "anthropic/claude-sonnet-5.5"
    assert await client.resolve_model_id(SONNET_ALIAS, job_id=7) == "anthropic/claude-sonnet-5.5"
    assert resolve.await_count == 1
    # A different job resolves for itself and sees where the alias points now.
    assert await client.resolve_model_id(SONNET_ALIAS, job_id=8) == "anthropic/claude-sonnet-6"


async def test_reset_job_models_lets_a_retry_resolve_afresh(monkeypatch):
    from api.services import model_roster

    resolve = AsyncMock(side_effect=["anthropic/claude-sonnet-5.5", "anthropic/claude-sonnet-6"])
    monkeypatch.setattr(model_roster, "resolve_alias", resolve)
    client = _openrouter_client()

    await client.resolve_model_id(SONNET_ALIAS, job_id=7)
    client.reset_job_models(7)
    assert await client.resolve_model_id(SONNET_ALIAS, job_id=7) == "anthropic/claude-sonnet-6"


async def test_unresolvable_alias_returns_none_and_is_not_pinned(monkeypatch):
    from api.services import model_roster

    resolve = AsyncMock(side_effect=[None, "anthropic/claude-sonnet-5.5"])
    monkeypatch.setattr(model_roster, "resolve_alias", resolve)
    client = _openrouter_client()

    assert await client.resolve_model_id(SONNET_ALIAS, job_id=7) is None
    assert await client.resolve_model_id(SONNET_ALIAS, job_id=7) == "anthropic/claude-sonnet-5.5"


async def test_alias_pins_are_bounded(monkeypatch):
    from api.services import llm as llm_mod
    from api.services import model_roster

    monkeypatch.setattr(model_roster, "resolve_alias", AsyncMock(return_value="anthropic/claude-sonnet-5.5"))
    monkeypatch.setattr(llm_mod, "MAX_PINNED_JOBS", 3)
    client = _openrouter_client()
    for job_id in range(5):
        await client.resolve_model_id(SONNET_ALIAS, job_id=job_id)
    assert list(client._job_model_pins) == [2, 3, 4]


async def test_chat_sends_the_resolved_id_never_the_alias(monkeypatch):
    """Guards, cost and the provider call all see the concrete model id."""
    from api.services import model_roster

    monkeypatch.setattr(model_roster, "resolve_alias", AsyncMock(return_value="anthropic/claude-sonnet-5.5"))
    client = _openrouter_client()
    client.config = {
        "phase_models": {"formatter": SONNET_ALIAS},
        "backends": {"openrouter": {"type": "openrouter", "endpoint": "https://openrouter.ai/x"}},
    }
    seen = {}

    def guard(name):
        return lambda model: seen.setdefault(name, model)

    monkeypatch.setattr(client, "check_run_cost_cap", lambda: None)
    monkeypatch.setattr(client, "check_model_allowed", guard("allowlist"))
    monkeypatch.setattr(client, "check_token_cost", guard("token_cost"))
    monkeypatch.setattr(client, "get_api_key", lambda cfg: "fake-key")

    class Stop(Exception):
        pass

    async def fake_call(config, model, messages, api_key, **kwargs):
        seen["call"] = model
        raise Stop

    monkeypatch.setattr(client, "_call_openrouter", fake_call)

    with pytest.raises(Stop):
        await client.chat(
            messages=[{"role": "user", "content": "hi"}], backend="openrouter", phase="formatter", job_id=3
        )

    assert seen == {key: "anthropic/claude-sonnet-5.5" for key in ("allowlist", "token_cost", "call")}
    assert client.active_model == "anthropic/claude-sonnet-5.5"


async def test_reasoning_reserve_only_for_mandatory_models(monkeypatch):
    from api.services import model_roster

    client = _openrouter_client()
    monkeypatch.setattr(model_roster, "reasoning_caps", AsyncMock(return_value=MANDATORY_CAPS))
    assert await client.reasoning_reserve("anthropic/claude-sonnet-5.5") == 8000
    client.config = {"routing": {"reasoning": {"reserve_tokens": 6000}}}
    assert await client.reasoning_reserve("anthropic/claude-sonnet-5.5") == 6000

    monkeypatch.setattr(model_roster, "reasoning_caps", AsyncMock(return_value=OPTIONAL_CAPS))
    assert await client.reasoning_reserve("anthropic/claude-sonnet-4.6") == 0
    monkeypatch.setattr(model_roster, "reasoning_caps", AsyncMock(return_value=None))
    assert await client.reasoning_reserve("anthropic/claude-sonnet-5.5") == 0


async def test_call_openrouter_raises_on_length_finish_reason(monkeypatch):
    """A provider-side length cutoff must fail the phase, not return success (#403).

    Job 48's formatter stopped mid-sentence at 706 words and was recorded as a
    completed phase because finish_reason was never inspected.
    """
    from api.services.llm import OutputTruncatedError

    client = LLMClient.__new__(LLMClient)
    client.active_backend = "openrouter"

    async def _post(endpoint, headers, payload):
        return FakeRespOK(finish_reason="length", content="And just a trifle to")

    monkeypatch.setattr(client, "_post_openrouter", _post)

    with pytest.raises(OutputTruncatedError):
        await client._call_openrouter(
            config={"endpoint": "https://openrouter.ai/api/v1/chat/completions"},
            model="anthropic/claude-sonnet-4.6",
            messages=[{"role": "user", "content": "hi"}],
            api_key="fake-key",
        )


async def test_call_openrouter_returns_normally_on_stop(monkeypatch):
    """A natural stop must still succeed — the guard must not over-trigger (#403)."""
    client = LLMClient.__new__(LLMClient)
    client.active_backend = "openrouter"

    async def _post(endpoint, headers, payload):
        return FakeRespOK(finish_reason="stop", content="a complete answer")

    monkeypatch.setattr(client, "_post_openrouter", _post)

    result = await client._call_openrouter(
        config={"endpoint": "https://openrouter.ai/api/v1/chat/completions"},
        model="anthropic/claude-sonnet-4.6",
        messages=[{"role": "user", "content": "hi"}],
        api_key="fake-key",
    )

    assert result.content == "a complete answer"


class FakeRespReasoning:
    """OpenRouter body carrying reasoning-token accounting.

    Modern Anthropic models reason by default and those tokens are drawn from
    max_tokens, so a large completion can contain little visible output (#403).
    """

    status_code = 200

    def __init__(self, content="hi", finish_reason="stop", reasoning_tokens=12575, completion_tokens=16384):
        self._content = content
        self._finish_reason = finish_reason
        self._reasoning_tokens = reasoning_tokens
        self._completion_tokens = completion_tokens
        self.text = "{}"

    def json(self):
        return {
            "model": "anthropic/claude-sonnet-5",
            "usage": {
                "prompt_tokens": 32816,
                "completion_tokens": self._completion_tokens,
                "total_tokens": 32816 + self._completion_tokens,
                "completion_tokens_details": {"reasoning_tokens": self._reasoning_tokens},
            },
            "choices": [{"message": {"content": self._content}, "finish_reason": self._finish_reason}],
        }

    def raise_for_status(self):
        return None


async def test_call_openrouter_forwards_reasoning_control(monkeypatch):
    """A caller's reasoning setting must reach the OpenRouter payload (#403).

    Disabling reasoning is the difference between a truncated formatter run and
    a clean one on reasoning-by-default models.
    """
    client = LLMClient.__new__(LLMClient)
    client.active_backend = "openrouter"

    captured = {}

    async def _post(endpoint, headers, payload):
        captured["payload"] = payload
        return FakeRespReasoning(reasoning_tokens=0, completion_tokens=100)

    monkeypatch.setattr(client, "_post_openrouter", _post)

    await client._call_openrouter(
        config={"endpoint": "https://openrouter.ai/api/v1/chat/completions"},
        model="anthropic/claude-sonnet-5",
        messages=[{"role": "user", "content": "hi"}],
        api_key="fake-key",
        reasoning={"enabled": False},
    )

    assert captured["payload"].get("reasoning") == {"enabled": False}


async def test_call_openrouter_captures_reasoning_tokens(monkeypatch):
    """reasoning_tokens must be recorded, not silently folded into output_tokens (#403).

    Without this the budget-eater is invisible: completion_tokens looks healthy
    while almost none of it is visible output.
    """
    client = LLMClient.__new__(LLMClient)
    client.active_backend = "openrouter"

    async def _post(endpoint, headers, payload):
        return FakeRespReasoning(reasoning_tokens=12575, completion_tokens=16384)

    monkeypatch.setattr(client, "_post_openrouter", _post)

    result = await client._call_openrouter(
        config={"endpoint": "https://openrouter.ai/api/v1/chat/completions"},
        model="anthropic/claude-sonnet-5",
        messages=[{"role": "user", "content": "hi"}],
        api_key="fake-key",
    )

    assert result.reasoning_tokens == 12575


async def test_call_openrouter_raises_on_null_content(monkeypatch):
    """A 200 whose content is null is a failure, not an empty phase (#403).

    Observed live: the whole completion went to reasoning and content came back
    null. Returning that as success would write an empty transcript.

    Raises OutputTruncatedError, not MalformedResponseError: chat() retries the
    latter up to MALFORMED_BODY_MAX_ATTEMPTS for transient body corruption, and
    this condition is deterministic — retrying bills three identical
    completions and loses the remedy message (#403 review).
    """
    from api.services.llm import OutputTruncatedError

    client = LLMClient.__new__(LLMClient)
    client.active_backend = "openrouter"

    async def _post(endpoint, headers, payload):
        return FakeRespReasoning(content=None, finish_reason="stop")

    monkeypatch.setattr(client, "_post_openrouter", _post)

    with pytest.raises(OutputTruncatedError):
        await client._call_openrouter(
            config={"endpoint": "https://openrouter.ai/api/v1/chat/completions"},
            model="anthropic/claude-sonnet-5",
            messages=[{"role": "user", "content": "hi"}],
            api_key="fake-key",
        )


async def test_truncated_call_carries_its_cost(monkeypatch):
    """A length stop must carry the spend it already incurred (#403 review).

    `cost` is computed from real usage before the finish_reason check. Dropping
    it meant a truncated call — job 48 billed 1251 output tokens — never reached
    the run cost tracker, because chat() only books on a successful return. The
    run cost cap and the paused job's actual_cost both undercounted.
    """
    from api.services.llm import OutputTruncatedError

    client = LLMClient.__new__(LLMClient)
    client.active_backend = "openrouter"

    async def _post(endpoint, headers, payload):
        return FakeRespReasoning(finish_reason="length", completion_tokens=16384, reasoning_tokens=12575)

    monkeypatch.setattr(client, "_post_openrouter", _post)

    with pytest.raises(OutputTruncatedError) as exc:
        await client._call_openrouter(
            config={"endpoint": "https://openrouter.ai/api/v1/chat/completions"},
            model="anthropic/claude-sonnet-5",
            messages=[{"role": "user", "content": "hi"}],
            api_key="fake-key",
        )

    assert exc.value.cost > 0, "the tokens were billed; the cost must travel with the error"
    assert exc.value.output_tokens == 16384


def test_tracker_books_unusable_call():
    """Billed-but-unusable spend lands in the run totals (#403 review)."""
    from api.services.llm import RunCostTracker

    tracker = RunCostTracker()
    tracker.add_unusable_call(cost=0.25, output_tokens=16384, model="m", backend="openrouter")

    assert tracker.total_cost == 0.25
    assert tracker.total_output_tokens == 16384
    assert tracker.call_count == 1
    assert tracker.calls[0]["unusable"] is True


class FakeRespBilled(FakeRespReasoning):
    """OpenRouter body with the billed amount in usage.cost, as OpenRouter sends it."""

    def __init__(self, cost, **kw):
        super().__init__(**kw)
        self._cost = cost

    def json(self):
        body = super().json()
        body["usage"]["cost"] = self._cost
        return body


async def test_openrouter_cost_comes_from_usage_cost(monkeypatch):
    """The recorded cost is what OpenRouter billed, not the MODEL_PRICING guess.

    Reading usage.total_cost (a field OpenRouter never sends) priced every
    5.5-family call at the flat $1/$3 fallback on cardigan02 (job 27).
    """
    client = LLMClient.__new__(LLMClient)
    client.active_backend = "openrouter"

    async def _post(endpoint, headers, payload):
        return FakeRespBilled(cost=0.0123, completion_tokens=2000, reasoning_tokens=500)

    monkeypatch.setattr(client, "_post_openrouter", _post)

    resp = await client._call_openrouter(
        config={"endpoint": "https://openrouter.ai/api/v1/chat/completions"},
        model="anthropic/claude-sonnet-5",
        messages=[{"role": "user", "content": "hi"}],
        api_key="fake-key",
    )

    assert resp.cost == pytest.approx(0.0123)


def test_reported_cost_reads_cost_then_total_cost():
    from api.services.llm import _reported_cost

    assert _reported_cost({"cost": 0.5}) == 0.5
    assert _reported_cost({"cost": 0}) == 0.0
    assert _reported_cost({"total_cost": 0.25}) == 0.25
    assert _reported_cost({"cost": "0.5"}) is None
    assert _reported_cost({"cost": True}) is None
    assert _reported_cost({}) is None
    assert _reported_cost(None) is None
