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
