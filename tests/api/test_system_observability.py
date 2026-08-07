"""Tests for cross-container observability (#158, #179).

The API container cannot see the worker/watcher containers via pgrep/lsof, so
/system/status falls back to shared-DB heartbeats and /system/health reads a
worker-published LLM snapshot. These tests exercise that fallback logic with the
local process probes forced to "not found" (the prod/LXC shape).
"""

import json
import signal
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


class TestSystemStatusHeartbeat:
    """GET /api/system/status worker/watcher detection via DB heartbeat (#179)."""

    def test_worker_up_via_fresh_heartbeat_without_local_process(self):
        """A fresh DB heartbeat reports the worker running even when pgrep finds nothing."""
        with (
            patch("api.routers.system._check_port_in_use", return_value=1234),
            patch("api.routers.system._find_process", return_value=None),
            patch(
                "api.routers.system.database.get_heartbeat_age_seconds",
                new_callable=AsyncMock,
            ) as mock_age,
        ):
            # worker fresh (5s), watcher stale-missing (None)
            mock_age.side_effect = [5.0, None]
            response = client.get("/api/system/status")

        assert response.status_code == 200
        data = response.json()
        assert data["worker"]["running"] is True
        assert data["worker"]["pid"] is None  # cross-container: no local pid
        assert data["worker"]["heartbeat_age_seconds"] == 5.0
        assert data["watcher"]["running"] is False

    def test_worker_down_when_no_process_and_no_heartbeat(self):
        """No local process and no heartbeat -> reported down (no false green)."""
        with (
            patch("api.routers.system._check_port_in_use", return_value=1234),
            patch("api.routers.system._find_process", return_value=None),
            patch(
                "api.routers.system.database.get_heartbeat_age_seconds",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            response = client.get("/api/system/status")

        assert response.status_code == 200
        data = response.json()
        assert data["worker"]["running"] is False
        assert data["watcher"]["running"] is False

    def test_stale_heartbeat_reports_down(self):
        """A heartbeat older than the stale window is not considered alive."""
        with (
            patch("api.routers.system._check_port_in_use", return_value=1234),
            patch("api.routers.system._find_process", return_value=None),
            patch(
                "api.routers.system.database.get_heartbeat_age_seconds",
                new_callable=AsyncMock,
                return_value=9999.0,
            ),
        ):
            response = client.get("/api/system/status")

        assert response.json()["worker"]["running"] is False

    def test_local_process_still_reports_up_in_dev(self):
        """Single-host dev: pgrep finds the process even with no heartbeat."""
        with (
            patch("api.routers.system._check_port_in_use", return_value=1234),
            patch("api.routers.system._find_process", return_value=4321),
            patch(
                "api.routers.system.database.get_heartbeat_age_seconds",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            response = client.get("/api/system/status")

        data = response.json()
        assert data["worker"]["running"] is True
        assert data["worker"]["pid"] == 4321


class TestWatcherHeartbeatEndpoint:
    """POST /api/system/watcher/heartbeat records watcher liveness (#179)."""

    def test_records_heartbeat(self):
        with patch("api.routers.system.database.record_heartbeat", new_callable=AsyncMock) as mock_record:
            response = client.post("/api/system/watcher/heartbeat")

        assert response.status_code == 200
        assert response.json()["success"] is True
        mock_record.assert_awaited_once_with("watcher")


class TestHealthLLMSnapshot:
    """GET /api/system/health prefers the worker-published DB snapshot (#158)."""

    def test_health_uses_db_runtime_when_inprocess_null(self):
        """active_backend/model/last_run come from the DB snapshot when the API
        process has none (the multi-container case)."""
        in_process_status = {
            "active_backend": None,
            "active_model": None,
            "primary_backend": "openrouter",
            "fallback_model": "anthropic/claude-sonnet-4",
            "phase_backends": {"analyst": "openrouter"},
            "last_run_totals": None,
        }
        runtime_snapshot = MagicMock()
        runtime_snapshot.value = json.dumps(
            {
                "active_backend": "openrouter",
                "active_model": "anthropic/claude-opus-4",
                "last_run_totals": {"cost": 0.1359},
            }
        )

        mock_client = MagicMock()
        mock_client.get_status.return_value = in_process_status

        async def fake_get_config(key):
            return runtime_snapshot if key == "llm_runtime_status" else None

        with (
            patch("api.main.get_llm_client", return_value=mock_client),
            patch("api.main.database.get_config", side_effect=fake_get_config),
            patch("api.main.database.list_jobs", new_callable=AsyncMock, return_value=[]),
        ):
            response = client.get("/api/system/health")

        assert response.status_code == 200
        data = response.json()
        assert data["llm"]["active_backend"] == "openrouter"
        assert data["llm"]["active_model"] == "anthropic/claude-opus-4"
        assert data["last_run"] == {"cost": 0.1359}
        # config-derived fields still come from the in-process client
        assert data["llm"]["primary_backend"] == "openrouter"


class TestRestartAction:
    """POST /api/system/restart writes the signal, reports live components, schedules self-exit."""

    def test_restart_writes_signal_and_reports_live_components(self):
        with (
            patch("api.routers.system._self_restart", new_callable=AsyncMock) as mock_self,
            patch(
                "api.routers.system.request_restart",
                new_callable=AsyncMock,
                return_value="2026-07-16T20:00:00+00:00",
            ) as mock_req,
            patch(
                "api.routers.system.database.get_heartbeat_age_seconds",
                new_callable=AsyncMock,
            ) as mock_age,
        ):
            mock_age.side_effect = [5.0, None]  # worker fresh, watcher absent
            response = client.post("/api/system/restart")

        assert response.status_code == 200
        data = response.json()
        assert data["requested_at"] == "2026-07-16T20:00:00+00:00"
        assert data["components"] == ["api", "worker"]
        mock_req.assert_awaited_once()
        mock_self.assert_awaited_once()  # self-restart scheduled + run as a background task


@pytest.mark.asyncio
async def test_self_restart_sends_sigterm_to_self():
    from api.routers.system import _self_restart

    with (
        patch("api.routers.system.asyncio.sleep", new_callable=AsyncMock),
        patch("api.routers.system.os.kill") as mock_kill,
        patch("api.routers.system.os.getpid", return_value=4321),
    ):
        await _self_restart()
    mock_kill.assert_called_once_with(4321, signal.SIGTERM)


class TestStatusContainerNames:
    def test_status_reports_api_running_and_container_names(self):
        with (
            patch("api.routers.system._check_port_in_use", return_value=None),
            patch("api.routers.system._find_process", return_value=None),
            patch(
                "api.routers.system.database.get_heartbeat_age_seconds",
                new_callable=AsyncMock,
            ) as mock_age,
        ):
            mock_age.side_effect = [None, None]
            response = client.get("/api/system/status")

        assert response.status_code == 200
        data = response.json()
        assert data["api"]["running"] is True
        assert data["api"]["container"] == "cardigan-api"
        assert data["worker"]["container"] == "cardigan-worker"
        assert data["watcher"]["container"] is None


class TestWatcherHeartbeatRestart:
    def test_heartbeat_returns_restart_true_when_signal_is_newer(self):
        signal_time = datetime(2026, 7, 16, 20, 0, tzinfo=timezone.utc)
        with patch(
            "api.routers.system.get_restart_requested_at",
            new_callable=AsyncMock,
            return_value=signal_time,
        ):
            resp = client.post(
                "/api/system/watcher/heartbeat",
                json={"started_at": "2026-07-16T19:00:00+00:00"},  # started before signal
            )
        assert resp.status_code == 200
        assert resp.json()["restart"] is True

    def test_heartbeat_returns_restart_false_when_started_after_signal(self):
        signal_time = datetime(2026, 7, 16, 20, 0, tzinfo=timezone.utc)
        with patch(
            "api.routers.system.get_restart_requested_at",
            new_callable=AsyncMock,
            return_value=signal_time,
        ):
            resp = client.post(
                "/api/system/watcher/heartbeat",
                json={"started_at": "2026-07-16T21:00:00+00:00"},  # started after signal
            )
        assert resp.status_code == 200
        assert resp.json()["restart"] is False

    def test_heartbeat_without_body_still_records_and_no_restart(self):
        resp = client.post("/api/system/watcher/heartbeat")
        assert resp.status_code == 200
        assert resp.json()["restart"] is False
