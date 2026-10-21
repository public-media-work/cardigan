"""System management endpoints.

Provides status and a single "Restart Components" action:
- GET  /status              — liveness of api / worker / watcher
- POST /restart             — request a restart of all running components
- POST /watcher/heartbeat   — watcher liveness ping (+ restart flag back)

Restart uses "Option B": a single ``restart_requested_at`` timestamp in the
config KV table; each component compares it to its own process start time and
self-exits, and Docker's ``restart: unless-stopped`` policy brings it back. No
Docker socket. See docs/SYSTEM_RESTART_DESIGN.md.
"""

import asyncio
import os
import signal
import subprocess
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel

from api.services import database
from api.services.logging import get_logger
from api.services.restart_signal import (
    get_restart_requested_at,
    request_restart,
    should_restart,
)

logger = get_logger(__name__)

router = APIRouter()


class ComponentStatus(BaseModel):
    """Status of a system component."""

    name: str
    running: bool
    pid: Optional[int] = None
    # Seconds since the component's last DB heartbeat (None for API, which is
    # detected by port, or if the component has never heartbeated).
    heartbeat_age_seconds: Optional[float] = None
    # Docker container name for this component, or None if not containerized
    # (the transcript watcher has no container in the current compose).
    container: Optional[str] = None


class SystemStatus(BaseModel):
    """Status of all system components."""

    api: ComponentStatus
    worker: ComponentStatus
    watcher: ComponentStatus


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


def _find_process(pattern: str) -> Optional[int]:
    """Find PID of process matching pattern."""
    try:
        result = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            # Return first matching PID
            pids = result.stdout.strip().split("\n")
            return int(pids[0])
        return None
    except Exception:
        return None


def _check_port_in_use(port: int) -> Optional[int]:
    """Check if a port is in use and return the PID using it.

    More reliable than pgrep for detecting running servers,
    especially when the server process is the one calling this function.
    """
    try:
        result = subprocess.run(["lsof", "-t", "-i", f":{port}"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            # Return first PID using the port
            pids = result.stdout.strip().split("\n")
            return int(pids[0])
        return None
    except Exception:
        return None


@router.get("/status", response_model=SystemStatus)
async def get_system_status():
    """Get status of all system components.

    The API is running by definition — it is answering this request — so it is
    always reported up (the in-container port probe is unreliable, see #304).
    Worker/watcher are detected by a fresh DB heartbeat (works across container
    boundaries, #179) OR a local process (single-host dev).
    """
    # API answers its own request; the port probe is kept only as dev info.
    api_pid = _check_port_in_use(8000)

    # Worker/watcher: same-host process probe (dev) plus shared-DB heartbeat (prod).
    worker_pid = _find_process("run_worker.py")
    watcher_pid = _find_process("watch_transcripts.py")
    worker_age = await database.get_heartbeat_age_seconds("worker")
    watcher_age = await database.get_heartbeat_age_seconds("watcher")

    worker_running = worker_pid is not None or database.heartbeat_is_fresh(worker_age)
    watcher_running = watcher_pid is not None or database.heartbeat_is_fresh(watcher_age)

    return SystemStatus(
        api=ComponentStatus(name="API Server", running=True, pid=api_pid, container="cardigan-api"),
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


@router.post("/watcher/heartbeat", response_model=WatcherHeartbeatResponse)
async def watcher_heartbeat(body: Optional[WatcherHeartbeatRequest] = None):
    """Record watcher liveness and tell it whether a restart was requested.

    The watcher has no DB access; it reports its own boot time and the API
    computes the restart flag so the watcher can self-exit (its supervisor
    restarts it). Recording liveness is how /status detects it across
    container boundaries (#179).
    """
    await database.record_heartbeat("watcher")

    restart = False
    if body is not None and body.started_at:
        try:
            started = datetime.fromisoformat(body.started_at)
            # An offset-less timestamp parses fine but is naive, and comparing it
            # against our aware `requested_at` raises TypeError. Read it as UTC
            # rather than swallowing it: a caller that omits the offset should get
            # the restart it asked for, not a silent `restart: false`.
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            restart = should_restart(started, await get_restart_requested_at())
        except (ValueError, TypeError):
            restart = False

    return WatcherHeartbeatResponse(success=True, message="Watcher heartbeat recorded", restart=restart)


async def _self_restart(delay: float = 1.0) -> None:
    """Terminate this API process after the response has flushed.

    uvicorn handles SIGTERM as a graceful shutdown; ``restart: unless-stopped``
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

    # Trace the cycle. This endpoint replaces machinery that claimed success
    # while doing nothing (#304), so the request itself must leave a record --
    # otherwise "did the restart actually happen?" is unanswerable after the
    # fact, which is the same blind spot in a different place.
    logger.info(
        "Restart requested via Settings",
        extra={
            "event": "restart_requested",
            "requested_at": requested_at,
            "components": components,
        },
    )

    background_tasks.add_task(_self_restart)
    return RestartRequestResponse(
        requested_at=requested_at,
        components=components,
        message="Restart requested; components will cycle shortly.",
    )
