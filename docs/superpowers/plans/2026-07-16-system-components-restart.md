# System Components Restart — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a single "Restart Components" action to the Settings → System panel that actually cycles the running containers (api + worker) in the containerized deployment, replacing the #304 no-op/false-success endpoints.

**Architecture:** One timestamp in the existing config KV table (`restart_requested_at`) is the sole signal. Each long-lived component compares it against its own process start time and self-restarts when the signal is newer; `restart: unless-stopped` brings each container back. The API exits itself (respond-then-SIGTERM); the worker drains in-flight jobs (bounded by a 60s timeout) then exits; the watcher (dev-only, no container in prod) exits after its current scan. No Docker socket, no new privileges.

**Tech Stack:** Python 3.13 + FastAPI + SQLite (SQLAlchemy async), React + TypeScript + Vite, pytest + vitest.

**Reference spec:** `docs/superpowers/specs/2026-07-16-system-components-restart-design.md`

## Global Constraints

- Python: type hints required; lint with `ruff check`; test with `pytest`. TDD, frequent commits.
- No Docker socket mounted into app containers. Restart is via self-exit + `restart: unless-stopped`.
- Reuse the existing config KV (`api/services/database.py` `get_config`/`set_config`) — do **not** add a new table.
- `POST /api/system/restart` is a mutating endpoint and MUST NOT be added to the auth-exempt path list (exempt: `/`, `/api/system/health`, `/docs`, `/openapi.json`, `/api/ws/*`). It is thus protected by `X-API-Key` whenever `CARDIGAN_API_KEY` is set.
- Timestamps are ISO-8601 UTC (`datetime.now(timezone.utc).isoformat()`), compared as tz-aware `datetime`.
- Commit messages follow repo convention: a `type(scope): summary` subject plus the `[Agent: Main Assistant]` line and the standard `Co-Authored-By:` / `Claude-Session:` trailers.
- **Test-runner safety:** any test that exercises `POST /api/system/restart` MUST patch `api.routers.system._self_restart` (or `os.kill`) first — otherwise the endpoint's self-restart will SIGTERM the pytest process.

---

## File Structure

| File | Responsibility | Create/Modify |
|------|----------------|---------------|
| `api/services/restart_signal.py` | The signal: write a request, read it back, and the pure `should_restart()` comparison. Shared by router + worker + watcher-endpoint. | **Create** |
| `api/routers/system.py` | `POST /restart` (write signal + schedule API self-exit); `/status` fixes (api running, container names); `/watcher/heartbeat` accepts `started_at` and returns `restart`; remove the dead restart/stop/start endpoints + their helpers. | Modify |
| `api/services/worker.py` | Record start time; check the signal each poll and drain-then-exit; bound the shutdown drain with a timeout. | Modify |
| `watch_transcripts.py` | Send `started_at` in the heartbeat, read the `restart` flag, exit the loop when set. | Modify |
| `web/src/components/RestartComponentsButton.tsx` | Isolated button → inline confirm → calls `onConfirm`; shows "Restarting…". Testable without the Settings page. | **Create** |
| `web/src/pages/Settings.tsx` | Wire the button + `restartComponents()`; drive all three status rows from `systemStatus`; fix container badges; watcher "Not deployed". | Modify |
| `docs/AGENT_INTERFACE_GUIDE.md` | Document `POST /api/system/restart`; note removed endpoints. | Modify |

Test files: `tests/services/test_restart_signal.py` (create), `tests/api/test_system_observability.py` (extend), `tests/api/test_worker.py` (extend), `tests/test_watch_transcripts.py` (extend), `web/src/components/__tests__/RestartComponentsButton.test.tsx` (create).

---

## Task 1: Restart-signal service

**Files:**
- Create: `api/services/restart_signal.py`
- Test: `tests/services/test_restart_signal.py`

**Interfaces:**
- Consumes: `api.services.database.get_config`, `api.services.database.set_config`.
- Produces:
  - `async def request_restart() -> str` — writes `restart_requested_at = now`, returns the ISO string.
  - `async def get_restart_requested_at() -> Optional[datetime]` — parsed tz-aware datetime or `None`.
  - `def should_restart(start_time: datetime, requested_at: Optional[datetime]) -> bool` — pure.
  - `RESTART_KEY = "restart_requested_at"`.

- [ ] **Step 1: Write the failing test** (pure logic — no DB)

Create `tests/services/test_restart_signal.py`:

```python
from datetime import datetime, timezone

from api.services.restart_signal import should_restart

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 1, 2, tzinfo=timezone.utc)


def test_should_restart_true_when_request_is_newer_than_start():
    assert should_restart(start_time=T0, requested_at=T1) is True


def test_should_restart_false_when_request_predates_start():
    assert should_restart(start_time=T1, requested_at=T0) is False


def test_should_restart_false_when_no_request():
    assert should_restart(start_time=T0, requested_at=None) is False


def test_should_restart_false_when_request_equals_start():
    assert should_restart(start_time=T0, requested_at=T0) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/test_restart_signal.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'api.services.restart_signal'`

- [ ] **Step 3: Write the implementation**

Create `api/services/restart_signal.py`:

```python
"""Restart-signal helpers for the 'Restart Components' action.

A single timestamp in the config KV table (``restart_requested_at``) is the
whole signal. Each long-lived component compares it against its own process
start time and self-restarts when the signal is newer; because a restarted
process has a newer start time, the same signal never re-fires. See
docs/superpowers/specs/2026-07-16-system-components-restart-design.md.
"""

from datetime import datetime, timezone
from typing import Optional

from api.services import database

RESTART_KEY = "restart_requested_at"


async def request_restart() -> str:
    """Record a restart request (now, UTC ISO-8601) and return the timestamp."""
    now_iso = datetime.now(timezone.utc).isoformat()
    await database.set_config(
        RESTART_KEY,
        now_iso,
        value_type="string",
        description="UTC timestamp of the last 'Restart Components' request",
    )
    return now_iso


async def get_restart_requested_at() -> Optional[datetime]:
    """Return the last restart-request time, or None if never requested / unparseable."""
    item = await database.get_config(RESTART_KEY)
    if item is None or not item.value:
        return None
    try:
        return datetime.fromisoformat(item.value)
    except ValueError:
        return None


def should_restart(start_time: datetime, requested_at: Optional[datetime]) -> bool:
    """True if a restart was requested strictly after this process started."""
    if requested_at is None:
        return False
    return requested_at > start_time
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/services/test_restart_signal.py -v`
Expected: PASS (4 tests). The DB-backed `request_restart`/`get_restart_requested_at` wrappers are covered end-to-end in Tasks 2–3.

- [ ] **Step 5: Lint + commit**

```bash
ruff check api/services/restart_signal.py tests/services/test_restart_signal.py
git add api/services/restart_signal.py tests/services/test_restart_signal.py
git commit -m "feat(system): add restart-signal service (one-timestamp, N-consumers)"
```

---

## Task 2: System router — `/restart`, `/status` fixes, watcher heartbeat, remove dead endpoints

**Files:**
- Modify: `api/routers/system.py`
- Test: `tests/api/test_system_observability.py`

**Interfaces:**
- Consumes: `restart_signal.request_restart`, `restart_signal.get_restart_requested_at`, `restart_signal.should_restart`, `database.get_heartbeat_age_seconds`, `database.heartbeat_is_fresh`, `database.record_heartbeat`.
- Produces:
  - `POST /api/system/restart` → `RestartRequestResponse{requested_at: str, components: list[str], message: str}`.
  - `POST /api/system/watcher/heartbeat` accepts `WatcherHeartbeatRequest{started_at: Optional[str]}` → `WatcherHeartbeatResponse{success: bool, message: str, restart: bool}`.
  - `GET /api/system/status` → `SystemStatus` where each `ComponentStatus` gains `container: Optional[str]`, and `api.running` is `True`.
  - `async def _self_restart(delay: float = 1.0)` — sleeps then `os.kill(getpid, SIGTERM)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/api/test_system_observability.py`:

```python
import signal as signal_module
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest


class TestRestartAction:
    """POST /api/system/restart writes the signal, reports live components, schedules self-exit."""

    def test_restart_writes_signal_and_reports_live_components(self):
        with (
            patch("api.routers.system._self_restart", new_callable=AsyncMock) as mock_self,
            patch("api.routers.system.request_restart", new_callable=AsyncMock,
                  return_value="2026-07-16T20:00:00+00:00") as mock_req,
            patch("api.routers.system.database.get_heartbeat_age_seconds",
                  new_callable=AsyncMock) as mock_age,
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
    mock_kill.assert_called_once_with(4321, signal_module.SIGTERM)


class TestStatusContainerNames:
    def test_status_reports_api_running_and_container_names(self):
        with (
            patch("api.routers.system._check_port_in_use", return_value=None),
            patch("api.routers.system._find_process", return_value=None),
            patch("api.routers.system.database.get_heartbeat_age_seconds",
                  new_callable=AsyncMock) as mock_age,
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
        with patch("api.routers.system.get_restart_requested_at",
                   new_callable=AsyncMock, return_value=signal_time):
            resp = client.post(
                "/api/system/watcher/heartbeat",
                json={"started_at": "2026-07-16T19:00:00+00:00"},  # started before signal
            )
        assert resp.status_code == 200
        assert resp.json()["restart"] is True

    def test_heartbeat_returns_restart_false_when_started_after_signal(self):
        signal_time = datetime(2026, 7, 16, 20, 0, tzinfo=timezone.utc)
        with patch("api.routers.system.get_restart_requested_at",
                   new_callable=AsyncMock, return_value=signal_time):
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/api/test_system_observability.py -k "Restart or ContainerNames or WatcherHeartbeatRestart" -v`
Expected: FAIL — 404/422 for `/restart`, `KeyError: 'container'`, and `restart` missing from the heartbeat response.

- [ ] **Step 3: Edit the router — models, imports, endpoints**

In `api/routers/system.py`:

3a. Add imports near the top (after the existing `import os`, `import subprocess`):

```python
import asyncio
import signal
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, HTTPException

from api.services.restart_signal import (
    get_restart_requested_at,
    request_restart,
    should_restart,
)
```

3b. Add `container` to `ComponentStatus`:

```python
class ComponentStatus(BaseModel):
    """Status of a system component."""

    name: str
    running: bool
    pid: Optional[int] = None
    heartbeat_age_seconds: Optional[float] = None
    container: Optional[str] = None  # docker container name, or None if not containerized
```

3c. Add the new request/response models (near `RestartResponse`):

```python
class RestartRequestResponse(BaseModel):
    """Response from the 'Restart Components' action."""

    requested_at: str
    components: List[str]
    message: str


class WatcherHeartbeatRequest(BaseModel):
    """Body for the watcher heartbeat (the watcher reports its own boot time)."""

    started_at: Optional[str] = None


class WatcherHeartbeatResponse(BaseModel):
    """Heartbeat ack, plus a restart signal the watcher self-applies."""

    success: bool
    message: str
    restart: bool = False
```

3d. Replace the body of `get_system_status()`'s `return` to set `running=True` for the API and add container names:

```python
    return SystemStatus(
        api=ComponentStatus(
            name="API Server", running=True, pid=api_pid, container="cardigan-api"
        ),
        worker=ComponentStatus(
            name="Worker",
            running=worker_running,
            pid=worker_pid,
            heartbeat_age_seconds=worker_age,
            container="cardigan-worker",
        ),
        watcher=ComponentStatus(
            name="Transcript Watcher",
            running=watcher_running,
            pid=watcher_pid,
            heartbeat_age_seconds=watcher_age,
            container=None,
        ),
    )
```

3e. Replace `watcher_heartbeat()` with the body-aware version:

```python
@router.post("/watcher/heartbeat", response_model=WatcherHeartbeatResponse)
async def watcher_heartbeat(body: Optional[WatcherHeartbeatRequest] = None):
    """Record watcher liveness and tell it whether a restart was requested.

    The watcher has no DB access; it reports its own boot time and the API
    computes the restart flag so the watcher can self-exit (Docker/supervisor
    restarts it).
    """
    await database.record_heartbeat("watcher")

    restart = False
    if body is not None and body.started_at:
        try:
            started = datetime.fromisoformat(body.started_at)
            restart = should_restart(started, await get_restart_requested_at())
        except ValueError:
            restart = False

    return WatcherHeartbeatResponse(
        success=True, message="Watcher heartbeat recorded", restart=restart
    )
```

3f. Add the `/restart` endpoint and `_self_restart` helper:

```python
async def _self_restart(delay: float = 1.0) -> None:
    """Terminate this API process after the response has flushed.

    uvicorn handles SIGTERM as a graceful shutdown; `restart: unless-stopped`
    brings the container back. The delay lets the HTTP response reach the client.
    """
    await asyncio.sleep(delay)
    os.kill(os.getpid(), signal.SIGTERM)


@router.post("/restart", response_model=RestartRequestResponse)
async def restart_components(background_tasks: BackgroundTasks):
    """Request a restart of all running components.

    Writes one timestamp; the worker (and dev watcher) self-restart on their
    next loop, and this API process schedules its own SIGTERM after responding.
    """
    requested_at = await request_restart()

    components = ["api"]
    worker_age = await database.get_heartbeat_age_seconds("worker")
    if database.heartbeat_is_fresh(worker_age):
        components.append("worker")
    watcher_age = await database.get_heartbeat_age_seconds("watcher")
    if database.heartbeat_is_fresh(watcher_age):
        components.append("watcher")

    background_tasks.add_task(_self_restart)
    return RestartRequestResponse(
        requested_at=requested_at,
        components=components,
        message="Restart requested; components will cycle shortly.",
    )
```

3g. **Delete** the dead endpoints and their now-unused helpers: `restart_worker`, `restart_watcher`, `stop_worker`, `stop_watcher`, `start_worker`, `start_watcher`, and the helpers `_kill_process` and `_start_component`. Keep `_find_process` and `_check_port_in_use` (still used by `/status`). Confirm no other references remain:

```bash
rg -n "_kill_process|_start_component|/worker/restart|/watcher/restart|/worker/stop|/worker/start" api/ web/src/ tests/
```

Expected: only the (about-to-be-updated) test file or nothing.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/api/test_system_observability.py -v`
Expected: PASS (new tests + the pre-existing observability tests).

- [ ] **Step 5: Lint + commit**

```bash
ruff check api/routers/system.py tests/api/test_system_observability.py
git add api/routers/system.py tests/api/test_system_observability.py
git commit -m "feat(system): POST /api/system/restart + status/heartbeat updates; remove dead restart endpoints (#304)"
```

---

## Task 3: Worker self-restart on signal + bounded drain

**Files:**
- Modify: `api/services/worker.py`
- Test: `tests/api/test_worker.py`

**Interfaces:**
- Consumes: `restart_signal.get_restart_requested_at`, `restart_signal.should_restart`.
- Produces: `JobWorker._start_time: datetime`; `JobWorker._should_stop_for_restart() -> bool`; module constant `RESTART_DRAIN_TIMEOUT_SECONDS = 60`.

- [ ] **Step 1: Write the failing test**

Append to `tests/api/test_worker.py`:

```python
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from api.services.worker import JobWorker, WorkerConfig


@pytest.mark.asyncio
async def test_should_stop_for_restart_true_when_request_is_newer():
    worker = JobWorker(WorkerConfig())
    worker._start_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with patch(
        "api.services.worker.get_restart_requested_at",
        new_callable=AsyncMock,
        return_value=datetime(2026, 1, 2, tzinfo=timezone.utc),
    ):
        assert await worker._should_stop_for_restart() is True


@pytest.mark.asyncio
async def test_should_stop_for_restart_false_when_no_request():
    worker = JobWorker(WorkerConfig())
    with patch(
        "api.services.worker.get_restart_requested_at",
        new_callable=AsyncMock,
        return_value=None,
    ):
        assert await worker._should_stop_for_restart() is False


@pytest.mark.asyncio
async def test_should_stop_for_restart_false_when_request_predates_start():
    worker = JobWorker(WorkerConfig())
    worker._start_time = datetime(2026, 1, 2, tzinfo=timezone.utc)
    with patch(
        "api.services.worker.get_restart_requested_at",
        new_callable=AsyncMock,
        return_value=datetime(2026, 1, 1, tzinfo=timezone.utc),
    ):
        assert await worker._should_stop_for_restart() is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/api/test_worker.py -k should_stop_for_restart -v`
Expected: FAIL — `AttributeError: 'JobWorker' object has no attribute '_should_stop_for_restart'` (and no `get_restart_requested_at` to patch in the worker module).

- [ ] **Step 3: Edit the worker**

In `api/services/worker.py`:

3a. Add the import (with the other `api.services` imports):

```python
from api.services.restart_signal import get_restart_requested_at, should_restart
```

3b. Add a module constant near the top (after imports):

```python
# How long to let in-flight jobs drain on a restart before force-exiting.
# A wedged job is reclaimed afterward by database.reset_stale_jobs().
RESTART_DRAIN_TIMEOUT_SECONDS = 60
```

3c. In `JobWorker.__init__`, record the start time:

```python
    def __init__(self, config: Optional[WorkerConfig] = None):
        self.config = config or WorkerConfig()
        self.llm = get_llm_client()
        self.running = False
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._current_job_id: Optional[int] = None
        self._start_time = datetime.now(timezone.utc)
```

3d. Add the method (near `stop`):

```python
    async def _should_stop_for_restart(self) -> bool:
        """True if a Settings 'Restart Components' request postdates this worker's start."""
        return should_restart(self._start_time, await get_restart_requested_at())
```

3e. In `start()`, immediately after the heartbeat/status-publish `try/except` block (and before "Clean up completed tasks"), insert:

```python
                # Exit for a Settings-requested restart; the supervisor/Docker
                # restart policy brings us back with the fresh config.
                if await self._should_stop_for_restart():
                    logger.info(
                        "Restart requested via Settings; draining in-flight jobs and exiting",
                        extra={"worker_id": worker_id},
                    )
                    self.running = False
                    continue
```

3f. Replace the shutdown drain at the end of `start()` with a bounded version:

```python
        # Wait for active tasks on shutdown, bounded so a wedged job can't block restart.
        if active_tasks:
            logger.info(
                "Waiting for active jobs to complete on shutdown",
                extra={"worker_id": worker_id, "active_jobs": len(active_tasks)},
            )
            try:
                await asyncio.wait_for(
                    asyncio.gather(*active_tasks, return_exceptions=True),
                    timeout=RESTART_DRAIN_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Drain timeout after %ss; abandoning %d in-flight job(s) for reclaim by reset_stale_jobs",
                    RESTART_DRAIN_TIMEOUT_SECONDS,
                    len(active_tasks),
                    extra={"worker_id": worker_id},
                )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/api/test_worker.py -k should_stop_for_restart -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Lint + commit**

```bash
ruff check api/services/worker.py tests/api/test_worker.py
git add api/services/worker.py tests/api/test_worker.py
git commit -m "feat(worker): self-restart on Settings signal with bounded job drain"
```

---

## Task 4: Watcher self-exit on restart flag

**Files:**
- Modify: `watch_transcripts.py`
- Test: `tests/test_watch_transcripts.py`

**Interfaces:**
- Consumes: `POST /api/system/watcher/heartbeat` returning `{restart: bool}` (Task 2).
- Produces: `send_heartbeat() -> bool` (True ⇒ restart requested); module-level `START_TIME`; `watch_loop()` returns when a restart is requested.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_watch_transcripts.py` (module is imported as `watch_transcripts`):

```python
from unittest.mock import MagicMock, patch

import watch_transcripts


class TestHeartbeatRestart:
    @patch("watch_transcripts.httpx.post")
    def test_send_heartbeat_returns_true_when_restart_requested(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"restart": True})
        assert watch_transcripts.send_heartbeat() is True
        # posts its own boot time so the API can compute the flag
        _, kwargs = mock_post.call_args
        assert "started_at" in kwargs["json"]

    @patch("watch_transcripts.httpx.post")
    def test_send_heartbeat_returns_false_when_no_restart(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"restart": False})
        assert watch_transcripts.send_heartbeat() is False

    @patch("watch_transcripts.httpx.post", side_effect=Exception("network"))
    def test_send_heartbeat_returns_false_on_error(self, mock_post):
        assert watch_transcripts.send_heartbeat() is False

    @patch("watch_transcripts.time.sleep")
    @patch("watch_transcripts.queue_file")
    @patch("watch_transcripts.get_transcript_files", return_value=["new.srt"])
    @patch("watch_transcripts.get_queued_files", return_value=set())
    @patch("watch_transcripts.send_heartbeat", return_value=True)
    def test_watch_loop_exits_when_restart_requested(self, _hb, _q, _f, mock_queue, mock_sleep):
        # Returns (no infinite loop) because the heartbeat asked to restart —
        # and exits BEFORE processing files or sleeping.
        watch_transcripts.watch_loop()
        mock_queue.assert_not_called()
        mock_sleep.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_watch_transcripts.py -k "HeartbeatRestart" -v`
Expected: FAIL — `send_heartbeat` returns `None` (no bool) / doesn't send `started_at`; `watch_loop` ignores the flag and hangs (test times out or loops).

- [ ] **Step 3: Edit the watcher**

In `watch_transcripts.py`:

3a. Add near the top imports:

```python
from datetime import datetime, timezone

START_TIME = datetime.now(timezone.utc)
```

3b. Replace `send_heartbeat()`:

```python
def send_heartbeat() -> bool:
    """Ping the API for liveness; return True if a restart was requested.

    The watcher has no DB access, so it reports its own boot time and the API
    returns whether a Settings restart request postdates it. Best-effort: any
    failure returns False and never interrupts the loop.
    """
    try:
        resp = httpx.post(
            f"{API_BASE}/api/system/watcher/heartbeat",
            json={"started_at": START_TIME.isoformat()},
            timeout=5,
        )
        if resp.status_code == 200:
            return bool(resp.json().get("restart", False))
    except Exception:
        pass
    return False
```

3c. In `watch_loop()`, act on the return value (replace the bare `send_heartbeat()` call inside the `while True:` block):

```python
        while True:
            if send_heartbeat():
                print("[Watch] Restart requested via Settings; exiting for supervisor restart.")
                return

            current_files = set(get_transcript_files())
            new_files = current_files - seen_files

            for f in new_files:
                print(f"[Watch] New file detected: {f}")
                queue_file(f)

            seen_files = seen_files | current_files
            time.sleep(POLL_INTERVAL)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_watch_transcripts.py -k "HeartbeatRestart" -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Lint + commit**

```bash
ruff check watch_transcripts.py tests/test_watch_transcripts.py
git add watch_transcripts.py tests/test_watch_transcripts.py
git commit -m "feat(watcher): exit on restart flag from heartbeat response"
```

---

## Task 5: Frontend — Restart Components button + panel fixes

**Files:**
- Create: `web/src/components/RestartComponentsButton.tsx`
- Create: `web/src/components/__tests__/RestartComponentsButton.test.tsx`
- Modify: `web/src/pages/Settings.tsx`

**Interfaces:**
- Produces: `RestartComponentsButton({ onConfirm: () => void | Promise<void>, restarting: boolean })`.
- Consumes in Settings: `POST /api/system/restart`; `SystemStatus` rows with `container` field.

- [ ] **Step 1: Write the failing component test**

Create `web/src/components/__tests__/RestartComponentsButton.test.tsx`:

```tsx
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, test, expect, vi } from 'vitest'
import RestartComponentsButton from '../RestartComponentsButton'

describe('RestartComponentsButton', () => {
  test('requires confirmation before calling onConfirm', async () => {
    const onConfirm = vi.fn().mockResolvedValue(undefined)
    render(<RestartComponentsButton onConfirm={onConfirm} restarting={false} />)

    fireEvent.click(screen.getByRole('button', { name: /restart components/i }))
    expect(onConfirm).not.toHaveBeenCalled() // shows confirm step, does not fire yet

    fireEvent.click(screen.getByRole('button', { name: /^confirm$/i }))
    await waitFor(() => expect(onConfirm).toHaveBeenCalledTimes(1))
  })

  test('cancel aborts without calling onConfirm', () => {
    const onConfirm = vi.fn()
    render(<RestartComponentsButton onConfirm={onConfirm} restarting={false} />)
    fireEvent.click(screen.getByRole('button', { name: /restart components/i }))
    fireEvent.click(screen.getByRole('button', { name: /cancel/i }))
    expect(onConfirm).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: /restart components/i })).toBeInTheDocument()
  })

  test('shows a status message while restarting', () => {
    render(<RestartComponentsButton onConfirm={vi.fn()} restarting={true} />)
    expect(screen.getByRole('status')).toHaveTextContent(/restarting/i)
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx vitest run src/components/__tests__/RestartComponentsButton.test.tsx`
Expected: FAIL — cannot resolve `../RestartComponentsButton`.

- [ ] **Step 3: Create the component**

Create `web/src/components/RestartComponentsButton.tsx`:

```tsx
import { useState } from 'react'

interface Props {
  onConfirm: () => void | Promise<void>
  restarting: boolean
}

export default function RestartComponentsButton({ onConfirm, restarting }: Props) {
  const [confirming, setConfirming] = useState(false)

  if (restarting) {
    return (
      <span role="status" className="text-sm text-pbs-300">
        Restarting… reconnecting to the dashboard.
      </span>
    )
  }

  if (!confirming) {
    return (
      <button
        type="button"
        onClick={() => setConfirming(true)}
        className="px-4 py-2 text-sm font-medium bg-pbs-600 hover:bg-pbs-500 text-white rounded focus:outline-none focus:ring-2 focus:ring-pbs-400"
      >
        Restart Components
      </button>
    )
  }

  return (
    <div className="flex items-center gap-3">
      <span className="text-sm text-surface-300">
        Restart the API and worker? The dashboard will briefly disconnect.
      </span>
      <button
        type="button"
        onClick={() => {
          setConfirming(false)
          onConfirm()
        }}
        className="px-3 py-2 text-sm font-medium bg-red-600 hover:bg-red-500 text-white rounded focus:outline-none focus:ring-2 focus:ring-red-400"
      >
        Confirm
      </button>
      <button
        type="button"
        onClick={() => setConfirming(false)}
        className="px-3 py-2 text-sm text-surface-300 hover:text-white"
      >
        Cancel
      </button>
    </div>
  )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && npx vitest run src/components/__tests__/RestartComponentsButton.test.tsx`
Expected: PASS (3 tests).

- [ ] **Step 5: Wire into Settings.tsx**

5a. Add the import at the top of `web/src/pages/Settings.tsx`:

```tsx
import RestartComponentsButton from '../components/RestartComponentsButton'
```

5b. Extend the `ComponentStatus` interface (line ~55):

```tsx
interface ComponentStatus {
  name: string
  running: boolean
  pid: number | null
  container?: string | null
}
```

5c. Add restart state next to the other `useState` hooks (after `systemStatus`):

```tsx
  const [restarting, setRestarting] = useState(false)
```

5d. Add the `restartComponents` callback (after `fetchSystemStatus`):

```tsx
  const restartComponents = useCallback(async () => {
    setRestarting(true)
    try {
      await fetch('/api/system/restart', { method: 'POST' })
    } catch {
      // Expected: the API restarts itself and the socket may drop mid-request.
    }
    // The 5s status poll reflects each container cycling; clear the banner after
    // a grace period once the API is expected to be back.
    setTimeout(() => setRestarting(false), 20000)
  }, [])
```

5e. Replace the **API Server** row's static markup (lines ~723–739) to use `systemStatus` and the container badge:

```tsx
                {/* API Server */}
                <div className="p-4 bg-surface-900 rounded-lg">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center space-x-3">
                      <div className={`w-3 h-3 rounded-full ${systemStatus?.api.running !== false ? 'bg-status-completed' : 'bg-status-pending'}`} />
                      <div>
                        <div className="font-medium text-white">API Server</div>
                        <div className="text-sm text-surface-400">Running - Managed by Docker</div>
                      </div>
                    </div>
                    <div className="px-3 py-1 text-xs bg-pbs-500/20 text-pbs-400 border border-pbs-500/30 rounded">
                      Container: {systemStatus?.api.container ?? 'cardigan-api'}
                    </div>
                  </div>
                </div>
```

5f. In the **Worker** row, change only the badge line (line ~756):

```tsx
                      Container: {systemStatus?.worker.container ?? 'cardigan-worker'}
```

5g. In the **Watcher** row, change the status text to "Not deployed" when down, and fix the badge (lines ~768–776):

```tsx
                        <div className="text-sm text-surface-400">
                          {systemStatus?.watcher.running ? 'Running - Managed by Docker' : 'Not deployed'}
                        </div>
```
```tsx
                      Container: {systemStatus?.watcher.container ?? '—'}
```

5h. Add the button to the System Components card, right after the closing `</div>` of the `space-y-4` rows list and before the card's closing `</div>` (i.e. after line ~780):

```tsx
              <div className="mt-6">
                <RestartComponentsButton onConfirm={restartComponents} restarting={restarting} />
              </div>
```

5i. In the "Docker Commands" reference card, **remove** the now-redundant "Restart all services" entry (lines ~819–822), leaving logs / stop / rebuild.

- [ ] **Step 6: Typecheck, build, and full web test run**

```bash
cd web && npm run build && npx vitest run
```
Expected: `tsc` passes (no type errors), Vite build succeeds, vitest green.

- [ ] **Step 7: Commit**

```bash
git add web/src/components/RestartComponentsButton.tsx web/src/components/__tests__/RestartComponentsButton.test.tsx web/src/pages/Settings.tsx
git commit -m "feat(web): Restart Components button + accurate System panel status"
```

---

## Task 6: Docs + full-suite gate

**Files:**
- Modify: `docs/AGENT_INTERFACE_GUIDE.md`

- [ ] **Step 1: Document the endpoint**

In `docs/AGENT_INTERFACE_GUIDE.md`, add `POST /api/system/restart` to the system endpoints section:

```markdown
- `POST /api/system/restart` — Request a restart of running components (api + worker; dev watcher if present). Writes a single `restart_requested_at` signal; each component self-restarts and `restart: unless-stopped` brings it back. Returns `{ requested_at, components, message }`. Mutating → requires `X-API-Key` when auth is enabled.
```

Also remove any documented references to the deleted `/api/system/{worker,watcher}/{restart,stop,start}` endpoints:

```bash
rg -n "system/(worker|watcher)/(restart|stop|start)" docs/
```

- [ ] **Step 2: Run the full backend suite + lint**

```bash
pytest -q
ruff check api/ tests/ watch_transcripts.py
```
Expected: green.

- [ ] **Step 3: Commit**

```bash
git add docs/AGENT_INTERFACE_GUIDE.md
git commit -m "docs(system): document POST /api/system/restart; drop dead endpoints"
```

---

## Manual Verification (post-deploy, on `cardigan01`)

The self-exit → Docker-restart loop can only be validated live (Docker's restart policy isn't unit-testable). After the branch is deployed as `:latest`:

1. In Settings → System, note the worker's status dot (green).
2. Change a worker config value (e.g. concurrency) in Settings → Worker and save.
3. Click **Restart Components → Confirm**. Observe: the dashboard briefly disconnects, the worker dot goes stale then fresh within ~1–2 poll cycles, and the API returns.
4. Confirm the worker came back with the new config in effect (via `/api/system/health` `llm`/worker snapshot or a new job honoring the changed concurrency).
5. Confirm no job was lost: any job that was in-flight either completed (graceful drain) or returned to `pending` via `reset_stale_jobs`.

---

## Self-Review

- **Spec coverage:** one-timestamp mechanism (Task 1) ✓; API respond-then-exit (Task 2) ✓; worker drain-with-timeout (Task 3) ✓; watcher heartbeat-flag exit (Task 4) ✓; single fire-and-forget UI action + reconnect (Task 5) ✓; `api running=true` + container labels + watcher "not deployed" (Tasks 2 & 5) ✓; remove no-op/false-success endpoints (Task 2) ✓; auth via existing middleware (Global Constraints) ✓; testing strategy (each task) ✓; manual live check (Verification) ✓.
- **Placeholder scan:** no TBD/TODO; every code step shows the code; commands have expected output.
- **Type consistency:** `should_restart(start_time, requested_at)` used identically in Tasks 1/2/3; `container` field added in Task 2 and consumed in Task 5; `send_heartbeat() -> bool` produced in Task 4 matches its `watch_loop` consumer; `RestartComponentsButton` prop names match test and Settings usage.
- **Known simplification (not a gap):** the "Restarting…" banner clears on a 20s timer rather than watching for API liveness; the live status dots already show the true per-container state, so this is cosmetic. Noted in Task 5.
