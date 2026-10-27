#!/usr/bin/env python3
"""Monitor the health of one or more running Cardigan instances.

For every configured instance this reports, per service, whether it is online and
working as expected, plus the current queue status:

    * API      — /api/system/health returns status "ok" (+ version from /)
    * Worker   — /api/system/status: running with a fresh DB heartbeat (< 120s)
    * Watcher  — same, but informational unless the instance opts in ("watcher": true)
    * Web      — the SPA vhost (:3100) is reachable
    * Queue    — /api/queue/stats counts (falls back to /api/system/health)
    * mmingest — /api/mmingest/status crawler state (informational, shown if reachable)

Unlike the older ``scripts/status.sh`` (local-only, pgrep/lsof based, no notion of
``cardigan01``), this fans out over an instance list and works against remote LXC
containers by reading the API's cross-container heartbeat.

Usage
-----
    python scripts/monitor.py                 # one-shot, exits 0 if all healthy else 1
    python scripts/monitor.py --watch         # live refreshing dashboard (5s)
    python scripts/monitor.py --watch 10      # ... every 10s
    python scripts/monitor.py --json          # machine-readable
    python scripts/monitor.py --url http://cardigan02:8100   # ad-hoc target(s)

Instances come from ``config/instances.json`` when present (see that file), otherwise
from built-in defaults (prod + localhost). Auth: an ``X-API-Key`` header is sent
when a key resolves (per-instance ``api_key_env``, else global ``CARDIGAN_API_KEY``);
authed endpoints degrade gracefully to "auth required" on a 401/403.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import httpx

# Ensure the project root is on the path when run directly (matches sibling scripts).
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# Mirrors api.services.database.HEARTBEAT_STALE_SECONDS (the source of truth). Duplicated
# here so this client doesn't import the server's DB stack just to read one constant.
HEARTBEAT_STALE_SECONDS = 120

# A healthy full mmingest crawl pass is ~24 min, so a 'running' crawl is only worth
# flagging as a possible stall once it has been running noticeably longer than that.
MMINGEST_STALL_MINUTES = 45

DEFAULT_INSTANCES = [
    # The service name, not a host: Caddy serves the SPA and proxies /api/ on
    # one origin, so there is no :8100 to derive the web URL from.
    {
        "name": "prod",
        "url": "https://cardigan.riechers.co",
        "web_url": "https://cardigan.riechers.co",
        "watcher": False,
    },
    {"name": "dev", "url": "http://localhost:8100", "watcher": True},
]

_QUEUE_KEYS = ("pending", "in_progress", "completed", "failed", "cancelled", "paused", "total")


class Health(str, Enum):
    UP = "up"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"


@dataclass
class ServiceHealth:
    name: str
    state: Health
    detail: str = ""


@dataclass
class QueueStatus:
    counts: dict
    source: str  # "queue/stats", "system/health (partial)"
    attention: bool = False  # failed > 0 or paused > 0


@dataclass
class InstanceHealth:
    name: str
    url: str
    verdict: Health
    services: list[ServiceHealth]
    queue: Optional[QueueStatus] = None
    version: Optional[str] = None
    restarted_at: Optional[str] = None
    version_deployed_at: Optional[str] = None
    llm: Optional[dict] = None
    mmingest: Optional[dict] = None
    notes: list[str] = field(default_factory=list)


# A GET result mirrors the (payload, status_code, error) convention used by
# mcp_server.server._mmingest_api_get: success -> (dict, 2xx, None); HTTP error ->
# (None, status, None); transport/JSON failure -> (None, None, message).
Probe = tuple[Optional[dict], Optional[int], Optional[str]]


@dataclass
class RawProbes:
    """The raw endpoint results for one instance — the sole input to classify()."""

    root: Probe
    health: Probe
    status: Probe
    queue: Probe
    mmingest: Probe
    web_reachable: Optional[bool]


# ---------------------------------------------------------------------------
# Instance loading
# ---------------------------------------------------------------------------


def _derive_web_url(api_url: str) -> Optional[str]:
    """Default web vhost URL: swap the API port (:8100) for the web port (:3100).

    Returns None when there's no ``:8100`` to swap (e.g. a proxied / Tailscale-Funnel
    host with no explicit port). We can't guess where the SPA lives in that case, so the
    caller skips the Web probe rather than probing the API URL and reporting a false
    "Web up" off the API's own response.
    """
    if ":8100" not in api_url:
        return None
    return api_url.replace(":8100", ":3100")


def _host_label(url: str) -> str:
    """A short instance name derived from a bare URL (host portion)."""
    return url.split("://", 1)[-1].split(":", 1)[0].split("/", 1)[0] or url


def _normalize(inst: dict) -> dict:
    inst = dict(inst)
    inst.setdefault("web_url", _derive_web_url(inst["url"]))
    inst.setdefault("watcher", False)
    return inst


def _default_config_path() -> str:
    return os.path.join(_project_root, "config", "instances.json")


def load_instances(
    config_path: Optional[str] = None,
    urls: Optional[list[str]] = None,
    api_key_env: Optional[str] = None,
) -> list[dict]:
    """Resolve the instance list: --url overrides config, config overrides defaults.

    Ad-hoc ``--url`` targets are marked ``adhoc`` so they never inherit the global
    ``CARDIGAN_API_KEY`` (see _resolve_key) — pass ``api_key_env`` (--api-key-env) to
    auth them explicitly. Malformed config rows (no ``url``) are skipped with a warning
    rather than aborting the whole run.
    """
    if urls:
        adhoc = {"adhoc": True, **({"api_key_env": api_key_env} if api_key_env else {})}
        return [_normalize({"name": _host_label(u), "url": u, **adhoc}) for u in urls]
    path = config_path or _default_config_path()
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
        valid = []
        for entry in data:
            if not isinstance(entry, dict) or not entry.get("url"):
                print(f"monitor: skipping malformed instance entry (no url): {entry!r}", file=sys.stderr)
                continue
            entry.setdefault("name", _host_label(entry["url"]))
            valid.append(_normalize(entry))
        return valid
    return [_normalize(i) for i in DEFAULT_INSTANCES]


def _resolve_key(instance: dict) -> Optional[str]:
    """Per-instance api_key_env (if set and populated), else global CARDIGAN_API_KEY.

    Ad-hoc ``--url`` instances never fall back to the global key — forwarding the
    production shared key to an operator-typed host would leak it.
    """
    env_name = instance.get("api_key_env")
    if env_name and os.environ.get(env_name):
        return os.environ[env_name]
    if instance.get("adhoc"):
        return None
    return os.environ.get("CARDIGAN_API_KEY")


# ---------------------------------------------------------------------------
# Probing (best-effort: one endpoint failing never sinks the rest)
# ---------------------------------------------------------------------------


async def _get(client: httpx.AsyncClient, base_url: str, path: str, headers: dict) -> Probe:
    url = f"{base_url.rstrip('/')}{path}"
    try:
        resp = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return None, None, type(exc).__name__
    if resp.status_code >= 400:
        return None, resp.status_code, None
    try:
        return resp.json(), resp.status_code, None
    except ValueError:
        return None, resp.status_code, "non-JSON response"


async def _reachable(client: httpx.AsyncClient, url: Optional[str]) -> Optional[bool]:
    """True/False reachability of ``url``; None when there is no URL to probe."""
    if not url:
        return None
    try:
        resp = await client.get(url)
    except httpx.HTTPError:
        return False
    return resp.status_code < 500


async def probe_instance(instance: dict, timeout: float) -> InstanceHealth:
    url = instance["url"]
    web_url = instance.get("web_url") or _derive_web_url(url)
    headers = {}
    key = _resolve_key(instance)
    if key:
        headers["X-API-Key"] = key

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        root, health, status, queue, mmingest, web_reachable = await asyncio.gather(
            _get(client, url, "/", headers),
            _get(client, url, "/api/system/health", headers),
            _get(client, url, "/api/system/status", headers),
            _get(client, url, "/api/queue/stats", headers),
            _get(client, url, "/api/mmingest/status", headers),
            _reachable(client, web_url),
        )

    raw = RawProbes(
        root=root, health=health, status=status, queue=queue, mmingest=mmingest, web_reachable=web_reachable
    )
    return classify(instance, raw)


def _error_instance(instance: dict, exc: BaseException) -> InstanceHealth:
    """A DOWN placeholder for an instance whose probe raised unexpectedly, so one bad
    entry degrades to a single red row instead of aborting the whole fan-out."""
    url = instance.get("url", "?")
    name = instance.get("name") or _host_label(url)
    return InstanceHealth(
        name=name,
        url=url,
        verdict=Health.DOWN,
        services=[ServiceHealth("API", Health.DOWN, f"probe error: {type(exc).__name__}")],
        notes=[f"probe raised {type(exc).__name__}: {exc}"],
    )


async def probe_all(instances: list[dict], timeout: float) -> list[InstanceHealth]:
    results = await asyncio.gather(*(probe_instance(i, timeout) for i in instances), return_exceptions=True)
    return [
        _error_instance(inst, res) if isinstance(res, BaseException) else res for inst, res in zip(instances, results)
    ]


# ---------------------------------------------------------------------------
# Classification (pure — unit-testable without a network)
# ---------------------------------------------------------------------------


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp (tolerating a trailing 'Z'); None if unparseable."""
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _component_health(label: str, comp: Optional[dict]) -> ServiceHealth:
    """Map a /api/system/status ComponentStatus dict to a ServiceHealth."""
    if not comp:
        return ServiceHealth(label, Health.UNKNOWN, "not reported")
    if not comp.get("running"):
        return ServiceHealth(label, Health.DOWN, "not running")
    age = comp.get("heartbeat_age_seconds")
    if age is None:
        # Running but no heartbeat published (e.g. the API component, detected by port).
        return ServiceHealth(label, Health.UP, "running")
    if age < HEARTBEAT_STALE_SECONDS:
        return ServiceHealth(label, Health.UP, f"running · hb {int(age)}s")
    return ServiceHealth(label, Health.DEGRADED, f"stale hb {int(age)}s (stuck?)")


def classify(instance: dict, raw: RawProbes, *, now: Optional[datetime] = None) -> InstanceHealth:
    """Turn raw endpoint results into a verdict for one instance. Pure function.

    ``now`` (UTC) is the reference time for age-based checks (e.g. crawl-stall). It
    defaults to the current time; tests pass it explicitly for determinism.
    """
    name = instance["name"]
    url = instance["url"]
    web_url = instance.get("web_url") or _derive_web_url(url)
    services: list[ServiceHealth] = []
    notes: list[str] = []

    # --- API + version ---
    health_payload, health_code, health_err = raw.health
    root_payload, _root_code, _root_err = raw.root
    version = root_payload.get("version") if root_payload else None
    if health_payload and health_payload.get("status") == "ok":
        api = ServiceHealth("API", Health.UP, f"ok{f' · v{version}' if version else ''}")
    elif health_code is not None:
        api = ServiceHealth("API", Health.DEGRADED, f"HTTP {health_code}")
    else:
        api = ServiceHealth("API", Health.DOWN, f"unreachable ({health_err or 'no response'})")
    services.append(api)

    # --- Worker & Watcher (from /api/system/status) ---
    status_payload, status_code, _status_err = raw.status
    if status_code in (401, 403):
        worker = ServiceHealth("Worker", Health.UNKNOWN, "auth required")
        watcher = _component_health("Watcher", None)
        watcher.state, watcher.detail = Health.UNKNOWN, "auth required"
    elif status_payload:
        worker = _component_health("Worker", status_payload.get("worker"))
        watcher = _component_health("Watcher", status_payload.get("watcher"))
    else:
        worker = ServiceHealth("Worker", Health.UNKNOWN, "status unavailable")
        watcher = ServiceHealth("Watcher", Health.UNKNOWN, "status unavailable")
    services.append(worker)

    watcher_required = bool(instance.get("watcher", False))
    watcher.name = "Watcher" if watcher_required else "Watcher (opt)"
    services.append(watcher)

    # --- Web SPA reachability ---
    if raw.web_reachable is None:
        web = ServiceHealth("Web", Health.UNKNOWN, "not checked")
    elif raw.web_reachable:
        web = ServiceHealth("Web", Health.UP, f"{web_url} reachable")
    else:
        web = ServiceHealth("Web", Health.DOWN, f"{web_url} unreachable")
    services.append(web)

    # --- Queue ---
    queue_payload, _queue_code, _queue_err = raw.queue
    queue: Optional[QueueStatus] = None
    if queue_payload:
        counts = {k: queue_payload.get(k, 0) for k in _QUEUE_KEYS}
        attention = bool(counts.get("failed", 0)) or bool(counts.get("paused", 0))
        queue = QueueStatus(counts=counts, source="queue/stats", attention=attention)
        # /api/queue/stats sums only 6 statuses; 'investigating' jobs are invisible here.
        notes.append("queue total excludes jobs in the 'investigating' state")
    elif health_payload and health_payload.get("queue"):
        q = health_payload["queue"]
        counts = {"pending": q.get("pending", 0), "in_progress": q.get("in_progress", 0)}
        queue = QueueStatus(counts=counts, source="system/health (partial)", attention=False)

    # --- Lifecycle markers (restart / deploy time; older instances omit them) ---
    instance_info = health_payload.get("instance") if health_payload else None
    restarted_at = instance_info.get("restarted_at") if instance_info else None
    version_deployed_at = instance_info.get("version_deployed_at") if instance_info else None

    # --- LLM (informational) ---
    llm = health_payload.get("llm") if health_payload else None

    # --- mmingest crawler (informational) ---
    mm_payload, _mm_code, _mm_err = raw.mmingest
    mmingest: Optional[dict] = None
    if mm_payload:
        last_run = mm_payload.get("last_run") or {}
        mmingest = {
            "running": mm_payload.get("running"),
            "counts": mm_payload.get("counts"),
            "last_run_status": last_run.get("status"),
        }
        # Only flag a running crawl once it has been going longer than a healthy pass —
        # a mid-crawl 'running' state is normal and shouldn't cry wolf.
        if last_run.get("status") == "running":
            started = _parse_iso(last_run.get("started_at"))
            if started is not None:
                age_min = ((now or datetime.now(timezone.utc)) - started).total_seconds() / 60
                if age_min > MMINGEST_STALL_MINUTES:
                    notes.append(
                        f"mmingest crawl running {int(age_min)} min (>{MMINGEST_STALL_MINUTES}) — possible stall"
                    )

    # --- Overall verdict ---
    # Deliberate: only a DOWN *API* makes the instance DOWN. A dead worker/web/watcher on a
    # still-answering API is DEGRADED, not DOWN — the box is reachable and inspectable, and
    # calling it DOWN would send an operator chasing a host that is in fact up. Note this is
    # per-instance; `_fleet_verdict` separately reports DOWN when every instance is down.
    # Exit-code contract is unaffected either way (0 iff all instances are UP).
    downgraders = [worker.state, web.state]
    if watcher_required:
        downgraders.append(watcher.state)
    if api.state == Health.DOWN:
        verdict = Health.DOWN
    elif api.state == Health.DEGRADED or any(s in (Health.DOWN, Health.DEGRADED) for s in downgraders):
        verdict = Health.DEGRADED
    else:
        verdict = Health.UP

    return InstanceHealth(
        name=name,
        url=url,
        verdict=verdict,
        services=services,
        queue=queue,
        version=version,
        restarted_at=restarted_at,
        version_deployed_at=version_deployed_at,
        llm=llm,
        mmingest=mmingest,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_STATE_GLYPH = {
    Health.UP: ("●", "green"),
    Health.DEGRADED: ("◐", "yellow"),
    Health.DOWN: ("○", "red"),
    Health.UNKNOWN: ("◌", "bright_black"),
}
_STATE_EMOJI = {
    Health.UP: "✅",
    Health.DEGRADED: "🟡",
    Health.DOWN: "❌",
    Health.UNKNOWN: "❔",
}


def _rich_available() -> bool:
    try:
        import rich  # noqa: F401
    except ImportError:
        return False
    return True


def _fleet_verdict(fleet: list[InstanceHealth]) -> str:
    if not fleet:
        return "UNKNOWN"
    if all(i.verdict == Health.UP for i in fleet):
        return "HEALTHY"
    # Only call the whole fleet DOWN when nothing is reachable; a mix (e.g. prod up,
    # local dev off) is DEGRADED rather than a false fleet-wide alarm.
    if all(i.verdict == Health.DOWN for i in fleet):
        return "DOWN"
    return "DEGRADED"


def _queue_summary_plain(counts: dict) -> str:
    parts = [f"{counts.get('pending', 0)} pending", f"{counts.get('in_progress', 0)} running"]
    if "completed" in counts:
        parts.append(f"{counts.get('completed', 0)} done")
    parts.append(f"{counts.get('failed', 0)} failed")
    parts.append(f"{counts.get('paused', 0)} paused")
    return " · ".join(parts)


def _fmt_ago(iso: Optional[str]) -> str:
    """Human 'time since' for an ISO timestamp, e.g. '2h ago'. 'n/a' if missing/unparseable."""
    dt = _parse_iso(iso)
    if dt is None:
        return "n/a"
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 0:
        return "just now"
    if secs < 90:
        return f"{int(secs)}s ago"
    if secs < 5400:  # 90 min
        return f"{int(secs / 60)}m ago"
    if secs < 129600:  # 36 h
        return f"{int(secs / 3600)}h ago"
    return f"{int(secs / 86400)}d ago"


def _lifecycle_parts(inst: InstanceHealth) -> list[str]:
    """Build 'restarted X · deployed Y' fragments (empty if the instance omits the markers)."""
    parts = []
    if inst.restarted_at:
        parts.append(f"restarted {_fmt_ago(inst.restarted_at)}")
    if inst.version_deployed_at:
        parts.append(f"deployed {_fmt_ago(inst.version_deployed_at)}")
    return parts


def render_plain(fleet: list[InstanceHealth], file=sys.stdout) -> None:
    print("🏘️  Cardigan Fleet — health & queue", file=file)
    for inst in fleet:
        print(f"\n{_STATE_EMOJI[inst.verdict]} {inst.name}   {inst.url}", file=file)
        for svc in inst.services:
            print(f"   {_STATE_EMOJI[svc.state]} {svc.name:<14} {svc.detail}", file=file)
        if inst.queue:
            print(f"   Queue: {_queue_summary_plain(inst.queue.counts)}  [{inst.queue.source}]", file=file)
        lc = _lifecycle_parts(inst)
        if lc:
            print(f"   ⏱ {' · '.join(lc)}", file=file)
        for note in inst.notes:
            print(f"   ⚠  {note}", file=file)
    healthy = sum(1 for i in fleet if i.verdict == Health.UP)
    print(f"\nOVERALL: {_fleet_verdict(fleet)} ({healthy}/{len(fleet)} healthy)", file=file)


def render_json(fleet: list[InstanceHealth]) -> str:
    return json.dumps([asdict(i) for i in fleet], indent=2, default=str)


def _queue_text(queue: QueueStatus):
    from rich.text import Text

    c = queue.counts
    t = Text()
    t.append(f"{c.get('pending', 0)} pending")
    t.append(" · ")
    t.append(f"{c.get('in_progress', 0)} running")
    if "completed" in c:
        t.append(" · ")
        t.append(f"{c.get('completed', 0)} done", style="bright_black")
    failed = c.get("failed", 0)
    t.append(" · ")
    t.append(f"{failed} failed", style="red" if failed else "bright_black")
    paused = c.get("paused", 0)
    t.append(" · ")
    t.append(f"{paused} paused", style="yellow" if paused else "bright_black")
    return t


def build_renderable(fleet: list[InstanceHealth], *, watch: bool = False):
    """Build a rich renderable for the whole fleet (used by one-shot and --watch)."""
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    panels = []
    for inst in fleet:
        glyph, color = _STATE_GLYPH[inst.verdict]
        title = Text.assemble((f"{glyph} ", color), (inst.name, "bold"), ("   ", ""), (inst.url, "bright_black"))

        grid = Table.grid(padding=(0, 2))
        grid.add_column(justify="left", no_wrap=True)
        grid.add_column(justify="left")
        for svc in inst.services:
            sglyph, scolor = _STATE_GLYPH[svc.state]
            grid.add_row(Text(f"{sglyph} {svc.name}", style=scolor), Text(svc.detail, style="bright_black"))
        if inst.queue:
            grid.add_row(Text("◆ Queue", style="cyan"), _queue_text(inst.queue))
        lc = _lifecycle_parts(inst)
        if lc:
            grid.add_row(Text("⏱ Lifecycle", style="magenta"), Text(" · ".join(lc), style="bright_black"))
        for note in inst.notes:
            grid.add_row(Text("⚠", style="yellow"), Text(note, style="yellow"))

        panels.append(Panel(grid, title=title, title_align="left", border_style=color, padding=(0, 1)))

    healthy = sum(1 for i in fleet if i.verdict == Health.UP)
    header = Text.assemble(
        ("🏘  Cardigan Fleet", "bold"),
        (f"   {_fleet_verdict(fleet)} · {healthy}/{len(fleet)} healthy", "bold"),
        (f"   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", "bright_black"),
        ("   (Ctrl-C to exit)" if watch else "", "bright_black"),
    )
    return Group(header, *panels)


async def run_watch(instances: list[dict], timeout: float, interval: float) -> None:
    from rich.console import Console
    from rich.live import Live

    console = Console()
    with Live(console=console, screen=True, auto_refresh=False, transient=True) as live:
        while True:
            fleet = await probe_all(instances, timeout)
            live.update(build_renderable(fleet, watch=True), refresh=True)
            await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Monitor Cardigan instances — service health + queue status.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--url", action="append", metavar="URL", help="Ad-hoc instance URL (repeatable); overrides config"
    )
    parser.add_argument("--config", metavar="PATH", help="Path to an instances.json (default: config/instances.json)")
    parser.add_argument(
        "--api-key-env",
        metavar="ENV_VAR",
        help="Env var holding an API key for --url targets (ad-hoc targets don't inherit CARDIGAN_API_KEY)",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    parser.add_argument(
        "--watch",
        nargs="?",
        type=float,
        const=5.0,
        metavar="SECONDS",
        help="Live refreshing dashboard, every SECONDS (default 5)",
    )
    parser.add_argument("--timeout", type=float, default=4.0, metavar="SECONDS", help="Per-request timeout (default 4)")
    parser.add_argument("--plain", action="store_true", help="Plain-text output (no rich dependency)")
    args = parser.parse_args()

    instances = load_instances(args.config, args.url, args.api_key_env)

    # `is not None`, not truthiness: --watch 0 parses to 0.0, which is falsy and would
    # otherwise fall through to one-shot mode without saying so. A non-positive interval
    # is a user error (it would spin the probe loop with no delay), so reject it outright.
    if args.watch is not None:
        if args.watch <= 0:
            print(f"--watch needs a positive interval in seconds (got {args.watch:g}).", file=sys.stderr)
            sys.exit(2)
        if not _rich_available():
            print("--watch needs the 'rich' package (pip install rich).", file=sys.stderr)
            sys.exit(2)
        try:
            asyncio.run(run_watch(instances, args.timeout, args.watch))
        except KeyboardInterrupt:
            pass
        return

    fleet = asyncio.run(probe_all(instances, args.timeout))

    if args.json:
        print(render_json(fleet))
    elif args.plain or not _rich_available():
        render_plain(fleet)
    else:
        from rich.console import Console

        Console().print(build_renderable(fleet))

    # Cron/CI-friendly: 0 iff every instance is fully UP.
    sys.exit(0 if all(i.verdict == Health.UP for i in fleet) else 1)


if __name__ == "__main__":
    main()
