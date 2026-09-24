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
    """
    from api.services.llm import MalformedResponseError

    client = LLMClient.__new__(LLMClient)
    client.active_backend = "openrouter"

    async def _post(endpoint, headers, payload):
        return FakeRespReasoning(content=None, finish_reason="stop")

    monkeypatch.setattr(client, "_post_openrouter", _post)

    with pytest.raises(MalformedResponseError):
        await client._call_openrouter(
            config={"endpoint": "https://openrouter.ai/api/v1/chat/completions"},
            model="anthropic/claude-sonnet-5",
            messages=[{"role": "user", "content": "hi"}],
            api_key="fake-key",
        )
