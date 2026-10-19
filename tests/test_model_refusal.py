"""Classifier refusals (stop_reason "refusal") are their own pause, not truncation.

Claude 5.x and Fable models carry safety classifiers that answer with a normal
200 whose content is empty or partial. Before this, the OpenRouter path reported
that as "the whole completion went to reasoning; disable reasoning or raise
max_tokens" (wrong cause, wrong remedy), a partial-content refusal passed as a
successful phase, and the direct-Anthropic path crashed indexing content[0].
Seen live on the 6POL0216 supervised run (category reasoning_extraction).
"""

import pytest

from api.services.llm import LLMClient, ModelRefusalError, OutputTruncatedError

ENDPOINT = {"endpoint": "https://openrouter.ai/api/v1/chat/completions"}
MSGS = [{"role": "user", "content": "hi"}]


class FakeOpenRouterRefusal:
    status_code = 200
    text = "{}"

    def __init__(self, content=None, finish_reason="content_filter", native="refusal", stop_details=None):
        self._content = content
        self._finish_reason = finish_reason
        self._native = native
        self._stop_details = stop_details

    def json(self):
        choice = {
            "message": {"content": self._content},
            "finish_reason": self._finish_reason,
            "native_finish_reason": self._native,
        }
        if self._stop_details is not None:
            choice["stop_details"] = self._stop_details
        return {
            "model": "anthropic/claude-sonnet-5.5",
            "usage": {"prompt_tokens": 30000, "completion_tokens": 464, "total_tokens": 30464},
            "choices": [choice],
        }

    def raise_for_status(self):
        return None


def _openrouter_client(monkeypatch, resp):
    client = LLMClient.__new__(LLMClient)
    client.active_backend = "openrouter"

    async def _post(endpoint, headers, payload):
        return resp

    monkeypatch.setattr(client, "_post_openrouter", _post)
    return client


async def test_openrouter_refusal_with_null_content_is_a_refusal_not_truncation(monkeypatch):
    client = _openrouter_client(monkeypatch, FakeOpenRouterRefusal(content=None))
    with pytest.raises(ModelRefusalError) as exc:
        await client._call_openrouter(config=ENDPOINT, model="anthropic/claude-sonnet-5.5", messages=MSGS, api_key="k")
    assert "safety classifier" in exc.value.detail
    assert "disable reasoning" not in exc.value.detail
    # Still an OutputTruncatedError, so every existing route (pause, cost booking) applies.
    assert isinstance(exc.value, OutputTruncatedError)
    assert exc.value.cost > 0, "a refusal is billed; the spend must travel with the error"


async def test_openrouter_refusal_with_partial_content_is_not_a_success(monkeypatch):
    """Partial output before a mid-stream refusal must be discarded, not shipped."""
    client = _openrouter_client(monkeypatch, FakeOpenRouterRefusal(content="# Formatted Transcript\n**Shawn"))
    with pytest.raises(ModelRefusalError):
        await client._call_openrouter(config=ENDPOINT, model="anthropic/claude-sonnet-5.5", messages=MSGS, api_key="k")


@pytest.mark.parametrize("finish_reason,native", [("content_filter", None), ("stop", "refusal")])
async def test_either_openrouter_signal_is_enough(monkeypatch, finish_reason, native):
    client = _openrouter_client(
        monkeypatch, FakeOpenRouterRefusal(content="", finish_reason=finish_reason, native=native)
    )
    with pytest.raises(ModelRefusalError):
        await client._call_openrouter(config=ENDPOINT, model="anthropic/claude-sonnet-5.5", messages=MSGS, api_key="k")


async def test_openrouter_refusal_carries_category_when_passed_through(monkeypatch):
    details = {
        "type": "refusal",
        "category": "reasoning_extraction",
        "explanation": "This request was blocked as it seems to violate Anthropic's Terms of Service.",
    }
    client = _openrouter_client(monkeypatch, FakeOpenRouterRefusal(stop_details=details))
    with pytest.raises(ModelRefusalError) as exc:
        await client._call_openrouter(config=ENDPOINT, model="anthropic/claude-sonnet-5.5", messages=MSGS, api_key="k")
    assert exc.value.category == "reasoning_extraction"
    assert "reasoning_extraction" in exc.value.detail
    assert "Terms of Service" in exc.value.detail


async def test_openrouter_natural_stop_is_untouched(monkeypatch):
    client = _openrouter_client(
        monkeypatch, FakeOpenRouterRefusal(content="a complete answer", finish_reason="stop", native="end_turn")
    )
    result = await client._call_openrouter(
        config=ENDPOINT, model="anthropic/claude-sonnet-5.5", messages=MSGS, api_key="k"
    )
    assert result.content == "a complete answer"


class FakeAnthropicRefusal:
    status_code = 200
    text = "{}"

    def json(self):
        return {
            "content": [],
            "stop_reason": "refusal",
            "stop_details": {"type": "refusal", "category": "cyber", "explanation": "declined"},
            "usage": {"input_tokens": 100, "output_tokens": 0},
        }

    def raise_for_status(self):
        return None


async def test_anthropic_refusal_raises_instead_of_crashing(monkeypatch):
    client = LLMClient.__new__(LLMClient)
    client.active_backend = "anthropic"

    class _HTTP:
        async def post(self, *a, **k):
            return FakeAnthropicRefusal()

    async def _get_client():
        return _HTTP()

    monkeypatch.setattr(client, "get_client", _get_client)
    with pytest.raises(ModelRefusalError) as exc:
        await client._call_anthropic(
            config={"endpoint": "https://api.anthropic.com/v1/messages"},
            model="claude-sonnet-5-5",
            messages=MSGS,
            api_key="k",
        )
    assert exc.value.category == "cyber"


async def test_worker_routes_refusal_to_its_own_pause(monkeypatch):
    from api.services.worker import JobWorker

    worker = JobWorker()
    calls = []

    async def _refusal(job_id, detail):
        calls.append(("refusal", detail))

    async def _truncation(job_id, detail):
        calls.append(("truncation", detail))

    monkeypatch.setattr(worker, "_pause_for_refusal", _refusal)
    monkeypatch.setattr(worker, "_pause_for_output_truncation", _truncation)

    await worker._pause_for_terminal_output_problem(1, {"output_truncated": True, "model_refusal": True, "error": "r"})
    await worker._pause_for_terminal_output_problem(1, {"output_truncated": True, "error": "t"})
    assert calls == [("refusal", "r"), ("truncation", "t")]


async def test_refusal_pause_message_names_the_right_remedy(monkeypatch):
    import api.services.worker as worker_mod
    from api.services.worker import JobWorker

    captured = {}

    async def _pause(job_id, *, trigger, message, mark_escalated=False):
        captured.update(trigger=trigger, message=message)

    async def _end(job_id):
        return None

    monkeypatch.setattr(worker_mod, "pause_and_suggest", _pause)
    monkeypatch.setattr(worker_mod, "end_run_tracking", _end)

    await JobWorker()._pause_for_refusal(7, "The model's safety classifier declined this request.")
    assert captured["trigger"] == "refusal"
    assert "different model" in captured["message"]
    assert "max_tokens" not in captured["message"]
