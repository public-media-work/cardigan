"""Tests for LangfuseClient in api/services/langfuse_client.py.

Tests credential loading priority, HTTP client initialization,
trace generation, model stats parsing, cost lookups, and graceful
degradation when credentials are missing.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from api.services.langfuse_client import (
    LangfuseClient,
    ModelStatsResponse,
    get_langfuse_client,
)
from api.services.secrets import get_secret

# ---------------------------------------------------------------------------
# Credential loading (now tests api.services.secrets.get_secret)
# ---------------------------------------------------------------------------


class TestCredentialLoading:
    """Tests for get_secret() priority order (Docker file → env → Keychain)."""

    @patch.dict("os.environ", {"LANGFUSE_PUBLIC_KEY": "pk-from-env"}, clear=False)
    def test_env_takes_priority_over_keychain(self):
        """Environment variable should be returned even if Keychain has a value."""
        with patch("api.services.secrets._keychain_get_secret", lambda k: "pk-from-keychain"):
            result = get_secret("LANGFUSE_PUBLIC_KEY")
        assert result == "pk-from-env"

    @patch.dict("os.environ", {}, clear=True)
    def test_falls_back_to_keychain(self):
        """When env var is missing, should fall back to Keychain."""
        with patch("api.services.secrets._keychain_get_secret", lambda k: "pk-from-keychain"):
            result = get_secret("LANGFUSE_PUBLIC_KEY")
        assert result == "pk-from-keychain"

    @patch.dict("os.environ", {}, clear=True)
    def test_returns_none_when_nothing_available(self):
        """When neither env nor Keychain has the key, return None."""
        with patch("api.services.secrets._keychain_get_secret", lambda k: None):
            result = get_secret("LANGFUSE_PUBLIC_KEY")
        assert result is None

    @patch.dict("os.environ", {}, clear=True)
    def test_returns_none_when_keychain_unavailable(self):
        """When get-secret.sh is absent from PATH, warn and return None."""
        with patch("api.services.secrets.shutil.which", return_value=None):
            result = get_secret("LANGFUSE_PUBLIC_KEY")
        assert result is None

    @patch.dict("os.environ", {"MY_KEY": "env-val"}, clear=False)
    def test_env_value_returned_when_keychain_unavailable(self):
        """Env var returned even when get-secret.sh is absent from PATH."""
        with patch("api.services.secrets.shutil.which", return_value=None):
            result = get_secret("MY_KEY")
        assert result == "env-val"


# ---------------------------------------------------------------------------
# Client initialization
# ---------------------------------------------------------------------------


class TestClientInitialization:
    """Tests for LangfuseClient._ensure_initialized()."""

    @patch("api.services.langfuse_client._get_langfuse_credential")
    def test_initializes_with_valid_credentials(self, mock_cred):
        mock_cred.side_effect = lambda k: {
            "LANGFUSE_PUBLIC_KEY": "pk-test",
            "LANGFUSE_SECRET_KEY": "sk-test",
            "LANGFUSE_BASE_URL": "https://us.cloud.langfuse.com",
        }.get(k)

        client = LangfuseClient()
        assert client._ensure_initialized() is True
        assert client._http_client is not None
        assert client._host == "https://us.cloud.langfuse.com"
        assert client._init_error is None

    @patch("api.services.langfuse_client._get_langfuse_credential")
    def test_fails_without_public_key(self, mock_cred):
        mock_cred.side_effect = lambda k: {
            "LANGFUSE_SECRET_KEY": "sk-test",
        }.get(k)

        client = LangfuseClient()
        assert client._ensure_initialized() is False
        assert client._http_client is None
        assert client._init_error is not None

    @patch("api.services.langfuse_client._get_langfuse_credential")
    def test_fails_without_secret_key(self, mock_cred):
        mock_cred.side_effect = lambda k: {
            "LANGFUSE_PUBLIC_KEY": "pk-test",
        }.get(k)

        client = LangfuseClient()
        assert client._ensure_initialized() is False
        assert client._http_client is None

    @patch("api.services.langfuse_client._get_langfuse_credential")
    def test_defaults_host_when_not_set(self, mock_cred):
        mock_cred.side_effect = lambda k: {
            "LANGFUSE_PUBLIC_KEY": "pk-test",
            "LANGFUSE_SECRET_KEY": "sk-test",
        }.get(k)

        client = LangfuseClient()
        client._ensure_initialized()
        assert client._host == "https://cloud.langfuse.com"

    @patch("api.services.langfuse_client._get_langfuse_credential")
    def test_only_initializes_once(self, mock_cred):
        mock_cred.side_effect = lambda k: {
            "LANGFUSE_PUBLIC_KEY": "pk-test",
            "LANGFUSE_SECRET_KEY": "sk-test",
        }.get(k)

        client = LangfuseClient()
        client._ensure_initialized()
        client._ensure_initialized()
        # _get_langfuse_credential called 3 times on first init (pk, sk, url)
        # Second call short-circuits, so still 3.
        assert mock_cred.call_count == 3

    @patch("api.services.langfuse_client._get_langfuse_credential")
    def test_is_available_reflects_init(self, mock_cred):
        mock_cred.return_value = None
        client = LangfuseClient()
        assert client.is_available() is False

    @patch("api.services.langfuse_client._get_langfuse_credential")
    def test_get_status_when_unavailable(self, mock_cred):
        mock_cred.return_value = None
        client = LangfuseClient()
        status = client.get_status()
        assert status["available"] is False
        assert status["error"] is not None

    @patch("api.services.langfuse_client._get_langfuse_credential")
    def test_get_status_when_available(self, mock_cred):
        mock_cred.side_effect = lambda k: {
            "LANGFUSE_PUBLIC_KEY": "pk-test",
            "LANGFUSE_SECRET_KEY": "sk-test",
            "LANGFUSE_BASE_URL": "https://us.cloud.langfuse.com",
        }.get(k)

        client = LangfuseClient()
        status = client.get_status()
        assert status["available"] is True
        assert status["error"] is None
        assert status["host"] == "https://us.cloud.langfuse.com"


# ---------------------------------------------------------------------------
# trace_generation()
# ---------------------------------------------------------------------------


class TestTraceGeneration:
    """Tests for LangfuseClient.trace_generation()."""

    def _make_client(self):
        """Create a client with a mocked HTTP client."""
        client = LangfuseClient()
        client._initialized = True
        client._http_client = AsyncMock(spec=httpx.AsyncClient)
        client._host = "https://us.cloud.langfuse.com"
        return client

    @pytest.mark.asyncio
    async def test_sends_batch_ingestion_request(self):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        client._http_client.post = AsyncMock(return_value=mock_resp)

        trace_id = await client.trace_generation(
            name="test-gen",
            model="gpt-4",
            input_messages=[{"role": "user", "content": "hello"}],
            output="world",
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            cost=0.001,
            duration_ms=500,
            job_id=42,
            phase="analyst",
            backend="openrouter",
        )

        assert trace_id is not None
        client._http_client.post.assert_called_once()
        call_args = client._http_client.post.call_args
        assert call_args[0][0] == "/api/public/ingestion"

        payload = call_args[1]["json"]
        assert len(payload["batch"]) == 2
        assert payload["batch"][0]["type"] == "trace-create"
        assert payload["batch"][1]["type"] == "generation-create"

    @pytest.mark.asyncio
    async def test_batch_payload_structure(self):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        client._http_client.post = AsyncMock(return_value=mock_resp)

        await client.trace_generation(
            name="seo-gen",
            model="claude-sonnet",
            input_messages=[{"role": "user", "content": "test"}],
            output="result",
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            cost=0.01,
            duration_ms=1200,
            job_id=7,
            phase="seo",
        )

        batch = client._http_client.post.call_args[1]["json"]["batch"]

        # Trace event
        trace_body = batch[0]["body"]
        assert trace_body["name"] == "job-7"
        assert trace_body["userId"] == "job-7"
        assert trace_body["sessionId"] == "session-7"
        assert "editorial-assistant" in trace_body["tags"]
        assert "phase:seo" in trace_body["tags"]

        # Generation event
        gen_body = batch[1]["body"]
        assert gen_body["model"] == "claude-sonnet"
        assert gen_body["usage"] == {"input": 100, "output": 50, "total": 150}
        assert gen_body["traceId"] == trace_body["id"]

    @pytest.mark.asyncio
    async def test_uses_name_when_no_job_id(self):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        client._http_client.post = AsyncMock(return_value=mock_resp)

        await client.trace_generation(
            name="standalone-call",
            model="gpt-4",
            input_messages=[],
            output="",
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            cost=0.0,
            duration_ms=0,
        )

        batch = client._http_client.post.call_args[1]["json"]["batch"]
        trace_body = batch[0]["body"]
        assert trace_body["name"] == "standalone-call"
        assert trace_body["userId"] is None

    @pytest.mark.asyncio
    async def test_returns_none_on_http_error(self):
        client = self._make_client()
        client._http_client.post = AsyncMock(
            side_effect=httpx.HTTPStatusError("500", request=MagicMock(), response=MagicMock())
        )

        result = await client.trace_generation(
            name="fail",
            model="gpt-4",
            input_messages=[],
            output="",
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            cost=0.0,
            duration_ms=0,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_not_initialized(self):
        client = LangfuseClient()
        client._initialized = True  # Mark as initialized but no http_client
        client._http_client = None

        result = await client.trace_generation(
            name="noop",
            model="gpt-4",
            input_messages=[],
            output="",
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            cost=0.0,
            duration_ms=0,
        )
        assert result is None


# ---------------------------------------------------------------------------
# get_model_stats()
# ---------------------------------------------------------------------------


class TestGetModelStats:
    """Tests for LangfuseClient.get_model_stats()."""

    def _make_client(self):
        client = LangfuseClient()
        client._initialized = True
        client._http_client = AsyncMock(spec=httpx.AsyncClient)
        client._host = "https://us.cloud.langfuse.com"
        return client

    @pytest.mark.asyncio
    async def test_parses_metrics_response(self):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "data": [
                {
                    "providedModelName": "gpt-4",
                    "count_count": 10,
                    "sum_totalCost": 0.50,
                    "sum_totalTokens": 5000,
                },
                {
                    "providedModelName": "claude-sonnet",
                    "count_count": 5,
                    "sum_totalCost": 0.25,
                    "sum_totalTokens": 2500,
                },
            ]
        }
        client._http_client.get = AsyncMock(return_value=mock_resp)

        result = await client.get_model_stats(days=7)

        assert result is not None
        assert isinstance(result, ModelStatsResponse)
        assert len(result.models) == 2
        assert result.total_cost == pytest.approx(0.75)
        assert result.total_requests == 15
        # Sorted by cost descending
        assert result.models[0].model_name == "gpt-4"
        assert result.models[1].model_name == "claude-sonnet"

    @pytest.mark.asyncio
    async def test_handles_empty_data(self):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"data": []}
        client._http_client.get = AsyncMock(return_value=mock_resp)

        result = await client.get_model_stats()

        assert result is not None
        assert result.models == []
        assert result.total_cost == 0.0
        assert result.total_requests == 0

    @pytest.mark.asyncio
    async def test_handles_null_data(self):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"data": None}
        client._http_client.get = AsyncMock(return_value=mock_resp)

        result = await client.get_model_stats()

        assert result is not None
        assert result.models == []

    @pytest.mark.asyncio
    async def test_handles_missing_model_name(self):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"data": [{"count_count": 3, "sum_totalCost": 0.1, "sum_totalTokens": 100}]}
        client._http_client.get = AsyncMock(return_value=mock_resp)

        result = await client.get_model_stats()
        assert result.models[0].model_name == "unknown"

    @pytest.mark.asyncio
    async def test_respects_limit(self):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "data": [
                {"providedModelName": f"model-{i}", "count_count": 1, "sum_totalCost": 0.01, "sum_totalTokens": 10}
                for i in range(10)
            ]
        }
        client._http_client.get = AsyncMock(return_value=mock_resp)

        result = await client.get_model_stats(limit=3)
        assert len(result.models) == 3

    @pytest.mark.asyncio
    async def test_returns_none_on_http_error(self):
        client = self._make_client()
        client._http_client.get = AsyncMock(
            side_effect=httpx.HTTPStatusError("500", request=MagicMock(), response=MagicMock())
        )

        result = await client.get_model_stats()
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_not_initialized(self):
        client = LangfuseClient()
        client._initialized = True
        client._http_client = None

        result = await client.get_model_stats()
        assert result is None


# ---------------------------------------------------------------------------
# get_trace_cost()
# ---------------------------------------------------------------------------


class TestGetTraceCost:
    """Tests for LangfuseClient.get_trace_cost()."""

    def _make_client(self):
        client = LangfuseClient()
        client._initialized = True
        client._http_client = AsyncMock(spec=httpx.AsyncClient)
        client._host = "https://us.cloud.langfuse.com"
        return client

    @pytest.mark.asyncio
    async def test_sums_observation_costs(self):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "observations": [
                {"calculatedTotalCost": 0.003},
                {"calculatedTotalCost": 0.007},
            ]
        }
        client._http_client.get = AsyncMock(return_value=mock_resp)

        cost = await client.get_trace_cost("trace-abc-123")
        assert cost == pytest.approx(0.01)
        client._http_client.get.assert_called_once_with("/api/public/traces/trace-abc-123")

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_observations(self):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"observations": []}
        client._http_client.get = AsyncMock(return_value=mock_resp)

        cost = await client.get_trace_cost("trace-empty")
        assert cost == 0.0

    @pytest.mark.asyncio
    async def test_handles_null_observations(self):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"observations": None}
        client._http_client.get = AsyncMock(return_value=mock_resp)

        cost = await client.get_trace_cost("trace-null")
        assert cost == 0.0

    @pytest.mark.asyncio
    async def test_skips_observations_without_cost(self):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "observations": [
                {"calculatedTotalCost": 0.005},
                {"calculatedTotalCost": None},
                {"other_field": "no cost here"},
            ]
        }
        client._http_client.get = AsyncMock(return_value=mock_resp)

        cost = await client.get_trace_cost("trace-partial")
        assert cost == pytest.approx(0.005)

    @pytest.mark.asyncio
    async def test_returns_none_on_http_error(self):
        client = self._make_client()
        client._http_client.get = AsyncMock(
            side_effect=httpx.HTTPStatusError("404", request=MagicMock(), response=MagicMock())
        )

        result = await client.get_trace_cost("nonexistent-trace")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_not_initialized(self):
        client = LangfuseClient()
        client._initialized = True
        client._http_client = None

        result = await client.get_trace_cost("trace-id")
        assert result is None


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class TestSingleton:
    """Tests for get_langfuse_client() singleton."""

    def test_returns_same_instance(self):
        with patch("api.services.langfuse_client._langfuse_client", None):
            a = get_langfuse_client()
            # Manually set the module-level singleton so the second call returns the same
            with patch("api.services.langfuse_client._langfuse_client", a):
                b = get_langfuse_client()
        assert a is b

    def test_returns_langfuse_client_instance(self):
        with patch("api.services.langfuse_client._langfuse_client", None):
            client = get_langfuse_client()
        assert isinstance(client, LangfuseClient)
