"""Tests for the fleet health monitor's pure classification logic.

These exercise ``classify`` with synthetic endpoint payloads — no network, and no
dependency on ``rich`` (which the monitor imports lazily inside its renderers).
"""

import json
import sys
from datetime import datetime, timedelta, timezone

import pytest

import scripts.monitor as monitor
from scripts.monitor import Health, InstanceHealth, RawProbes, _fmt_ago, classify

# A healthy-looking instance definition.
INSTANCE = {"name": "dev", "url": "http://localhost:8100", "web_url": "http://localhost:3100", "watcher": True}

# Reusable probe fragments (payload, status_code, error).
ROOT_OK = ({"status": "ok", "version": "4.3.0"}, 200, None)
HEALTH_OK = ({"status": "ok", "queue": {"pending": 2, "in_progress": 1}, "llm": {"active_model": "x"}}, 200, None)
QUEUE_OK = (
    {"pending": 2, "in_progress": 1, "completed": 40, "failed": 0, "cancelled": 0, "paused": 0, "total": 43},
    200,
    None,
)
UNREACHABLE = (None, None, "ConnectError")
NOT_FOUND = (None, 404, None)


def _service(result: InstanceHealth, name: str) -> Health:
    for svc in result.services:
        if svc.name == name or svc.name.startswith(name):
            return svc.state
    raise AssertionError(f"service {name!r} not in {[s.name for s in result.services]}")


def _status(worker=None, watcher=None) -> tuple:
    return ({"api": {"name": "API", "running": True}, "worker": worker, "watcher": watcher}, 200, None)


def test_all_healthy_is_up():
    raw = RawProbes(
        root=ROOT_OK,
        health=HEALTH_OK,
        status=_status(
            worker={"running": True, "heartbeat_age_seconds": 5},
            watcher={"running": True, "heartbeat_age_seconds": 11},
        ),
        queue=QUEUE_OK,
        mmingest=(None, None, "ConnectError"),
        web_reachable=True,
    )
    result = classify(INSTANCE, raw)
    assert result.verdict == Health.UP
    assert result.version == "4.3.0"
    assert _service(result, "API") == Health.UP
    assert _service(result, "Worker") == Health.UP
    assert _service(result, "Web") == Health.UP
    assert result.queue is not None and result.queue.counts["total"] == 43


def test_stale_worker_heartbeat_is_degraded():
    raw = RawProbes(
        root=ROOT_OK,
        health=HEALTH_OK,
        status=_status(worker={"running": True, "heartbeat_age_seconds": 300}),
        queue=QUEUE_OK,
        mmingest=UNREACHABLE,
        web_reachable=True,
    )
    result = classify(INSTANCE, raw)
    assert _service(result, "Worker") == Health.DEGRADED
    assert result.verdict == Health.DEGRADED


def test_api_unreachable_is_down_and_does_not_crash():
    raw = RawProbes(
        root=UNREACHABLE,
        health=UNREACHABLE,
        status=UNREACHABLE,
        queue=UNREACHABLE,
        mmingest=UNREACHABLE,
        web_reachable=False,
    )
    result = classify(INSTANCE, raw)
    assert result.verdict == Health.DOWN
    assert _service(result, "API") == Health.DOWN
    # Worker/queue can't be observed when the host is unreachable — reported, not fatal.
    assert _service(result, "Worker") == Health.UNKNOWN
    assert result.queue is None


def test_watcher_optional_does_not_downgrade():
    """A cardigan01-style instance (watcher not required) stays UP when the watcher is absent."""
    instance = {"name": "cardigan01", "url": "http://cardigan01:8100", "watcher": False}
    raw = RawProbes(
        root=ROOT_OK,
        health=HEALTH_OK,
        status=_status(
            worker={"running": True, "heartbeat_age_seconds": 8},
            watcher={"running": False},
        ),
        queue=QUEUE_OK,
        mmingest=UNREACHABLE,
        web_reachable=True,
    )
    result = classify(instance, raw)
    assert _service(result, "Watcher") == Health.DOWN  # reported as down...
    assert result.verdict == Health.UP  # ...but doesn't sink the verdict (informational)


def test_watcher_required_downgrades_when_down():
    raw = RawProbes(
        root=ROOT_OK,
        health=HEALTH_OK,
        status=_status(
            worker={"running": True, "heartbeat_age_seconds": 8},
            watcher={"running": False},
        ),
        queue=QUEUE_OK,
        mmingest=UNREACHABLE,
        web_reachable=True,
    )
    result = classify(INSTANCE, raw)  # INSTANCE has watcher=True
    assert result.verdict == Health.DEGRADED


def test_auth_required_marks_worker_unknown_but_api_up():
    raw = RawProbes(
        root=ROOT_OK,
        health=HEALTH_OK,
        status=(None, 401, None),
        queue=(None, 401, None),
        mmingest=(None, 401, None),
        web_reachable=True,
    )
    result = classify(INSTANCE, raw)
    assert _service(result, "API") == Health.UP
    assert _service(result, "Worker") == Health.UNKNOWN
    assert _service(result, "Watcher") == Health.UNKNOWN
    # Auth-blocked components can't be proven down, so they don't downgrade the verdict.
    assert result.verdict == Health.UP


def test_queue_falls_back_to_health_when_stats_blocked():
    raw = RawProbes(
        root=ROOT_OK,
        health=HEALTH_OK,
        status=_status(worker={"running": True, "heartbeat_age_seconds": 5}),
        queue=(None, 401, None),
        mmingest=UNREACHABLE,
        web_reachable=True,
    )
    result = classify(INSTANCE, raw)
    assert result.queue is not None
    assert result.queue.source.startswith("system/health")
    assert result.queue.counts["pending"] == 2


def test_queue_from_stats_carries_investigating_caveat():
    raw = RawProbes(
        root=ROOT_OK,
        health=HEALTH_OK,
        status=_status(worker={"running": True, "heartbeat_age_seconds": 5}),
        queue=QUEUE_OK,
        mmingest=UNREACHABLE,
        web_reachable=True,
    )
    result = classify(INSTANCE, raw)
    assert any("investigating" in note for note in result.notes)


def test_failed_or_paused_jobs_flag_attention():
    queue_with_failures = (
        {"pending": 0, "in_progress": 0, "completed": 10, "failed": 3, "cancelled": 0, "paused": 1, "total": 14},
        200,
        None,
    )
    raw = RawProbes(
        root=ROOT_OK,
        health=HEALTH_OK,
        status=_status(worker={"running": True, "heartbeat_age_seconds": 5}),
        queue=queue_with_failures,
        mmingest=UNREACHABLE,
        web_reachable=True,
    )
    result = classify(INSTANCE, raw)
    assert result.queue is not None and result.queue.attention is True
    # Attention is informational, not a health failure on its own.
    assert result.verdict == Health.UP


NOW = datetime(2026, 7, 23, 0, 20, tzinfo=timezone.utc)


def _mmingest_running(started_at: str) -> tuple:
    return ({"running": True, "counts": {}, "last_run": {"status": "running", "started_at": started_at}}, 200, None)


def _base_raw(mmingest) -> RawProbes:
    return RawProbes(
        root=ROOT_OK,
        health=HEALTH_OK,
        status=_status(worker={"running": True, "heartbeat_age_seconds": 5}),
        queue=QUEUE_OK,
        mmingest=mmingest,
        web_reachable=True,
    )


def test_running_crawl_within_healthy_window_is_not_flagged():
    """A crawl running ~10 min (< 45) is normal — no stall note."""
    raw = _base_raw(_mmingest_running("2026-07-23T00:10:00Z"))  # 10 min before NOW
    result = classify(INSTANCE, raw, now=NOW)
    assert not any("mmingest" in note for note in result.notes)


def test_long_running_crawl_is_flagged_as_possible_stall():
    """A crawl running ~80 min (> 45) is flagged."""
    raw = _base_raw(_mmingest_running("2026-07-22T23:00:00Z"))  # 80 min before NOW
    result = classify(INSTANCE, raw, now=NOW)
    assert any("possible stall" in note for note in result.notes)


def test_completed_crawl_is_never_flagged():
    mmingest = (
        {"running": False, "counts": {}, "last_run": {"status": "completed", "started_at": "2026-07-22T23:00:00Z"}},
        200,
        None,
    )
    raw = _base_raw(mmingest)
    result = classify(INSTANCE, raw, now=NOW)
    assert not any("mmingest" in note for note in result.notes)


def test_instance_lifecycle_markers_flow_through():
    health = (
        {
            "status": "ok",
            "queue": {"pending": 0, "in_progress": 0},
            "instance": {
                "version": "4.3.1",
                "restarted_at": "2026-07-23T00:00:00Z",
                "version_deployed_at": "2026-07-20T00:00:00Z",
            },
        },
        200,
        None,
    )
    raw = RawProbes(
        root=ROOT_OK,
        health=health,
        status=_status(worker={"running": True, "heartbeat_age_seconds": 5}),
        queue=QUEUE_OK,
        mmingest=UNREACHABLE,
        web_reachable=True,
    )
    result = classify(INSTANCE, raw)
    assert result.restarted_at == "2026-07-23T00:00:00Z"
    assert result.version_deployed_at == "2026-07-20T00:00:00Z"


def test_instance_lifecycle_markers_absent_are_none():
    """Older instances (no 'instance' block in /health) report None, not a crash."""
    result = classify(INSTANCE, _base_raw(UNREACHABLE))  # HEALTH_OK has no 'instance' block
    assert result.restarted_at is None
    assert result.version_deployed_at is None


def test_fmt_ago_buckets():
    assert _fmt_ago(None) == "n/a"
    assert _fmt_ago("not-a-date") == "n/a"
    now = datetime.now(timezone.utc)
    assert _fmt_ago((now - timedelta(seconds=5)).isoformat()).endswith("s ago")
    assert _fmt_ago((now - timedelta(hours=2)).isoformat()).endswith("h ago")
    assert _fmt_ago((now - timedelta(days=3)).isoformat()).endswith("d ago")


# --- Fleet verdict (banner) -------------------------------------------------


def _inst(verdict: Health) -> InstanceHealth:
    return InstanceHealth(name="x", url="http://x:8100", verdict=verdict, services=[])


def test_fleet_verdict_all_up_is_healthy():
    assert monitor._fleet_verdict([_inst(Health.UP), _inst(Health.UP)]) == "HEALTHY"


def test_fleet_verdict_all_down_is_down():
    # Regression: this branch used to also return "DEGRADED" (a fully-down fleet
    # never read DOWN).
    assert monitor._fleet_verdict([_inst(Health.DOWN), _inst(Health.DOWN)]) == "DOWN"


def test_fleet_verdict_mixed_is_degraded():
    # Prod up + local dev off is DEGRADED, not a false fleet-wide DOWN alarm.
    assert monitor._fleet_verdict([_inst(Health.UP), _inst(Health.DOWN)]) == "DEGRADED"


def test_fleet_verdict_empty_is_unknown():
    assert monitor._fleet_verdict([]) == "UNKNOWN"


# --- Web URL derivation -----------------------------------------------------


def test_derive_web_url_swaps_api_port():
    assert monitor._derive_web_url("http://cardigan01:8100") == "http://cardigan01:3100"


def test_derive_web_url_none_for_portless_host():
    # A proxied / Funnel host with no :8100 can't be turned into a web URL — the caller
    # must skip the Web probe rather than hit the API URL and report a false "Web up".
    assert monitor._derive_web_url("https://cardigan01.tail1234.ts.net") is None


def test_prod_default_is_the_service_name_with_explicit_web_url():
    # One origin serves the SPA and /api/, so there's no :8100 to derive from;
    # the explicit web_url must survive _normalize or the Web probe is skipped.
    prod = monitor._normalize(dict(next(i for i in monitor.DEFAULT_INSTANCES if i["name"] == "prod")))
    assert prod["url"] == "https://cardigan.riechers.co"
    assert prod["web_url"] == "https://cardigan.riechers.co"


def test_web_unknown_when_not_checked_does_not_downgrade():
    raw = RawProbes(
        root=ROOT_OK,
        health=HEALTH_OK,
        status=_status(worker={"running": True, "heartbeat_age_seconds": 5}),
        queue=QUEUE_OK,
        mmingest=UNREACHABLE,
        web_reachable=None,  # web probe skipped (no derivable URL)
    )
    result = classify(INSTANCE, raw)
    assert _service(result, "Web") == Health.UNKNOWN
    assert result.verdict == Health.UP  # not-checked web can't be proven down


# --- API key resolution -----------------------------------------------------


def test_adhoc_url_does_not_inherit_global_key(monkeypatch):
    monkeypatch.setenv("CARDIGAN_API_KEY", "prod-secret")
    (inst,) = monitor.load_instances(urls=["http://some-other-host:8100"])
    assert monitor._resolve_key(inst) is None


def test_config_instance_inherits_global_key(monkeypatch):
    monkeypatch.setenv("CARDIGAN_API_KEY", "prod-secret")
    inst = monitor._normalize({"name": "c", "url": "http://cardigan01:8100"})
    assert monitor._resolve_key(inst) == "prod-secret"


def test_adhoc_api_key_env_is_used(monkeypatch):
    monkeypatch.delenv("CARDIGAN_API_KEY", raising=False)
    monkeypatch.setenv("MY_KEY", "abc123")
    (inst,) = monitor.load_instances(urls=["http://host:8100"], api_key_env="MY_KEY")
    assert monitor._resolve_key(inst) == "abc123"


# --- Malformed config + probe isolation -------------------------------------


def test_load_instances_skips_malformed_config_rows(tmp_path):
    cfg = tmp_path / "instances.json"
    cfg.write_text(
        json.dumps(
            [
                {"name": "ok", "url": "http://ok:8100"},
                {"name": "no-url"},  # missing url — skipped
                "not-a-dict",  # not even a dict — skipped
            ]
        )
    )
    result = monitor.load_instances(str(cfg))
    assert [i["name"] for i in result] == ["ok"]


def test_error_instance_is_down_with_note():
    inst = monitor._error_instance({"name": "bad", "url": "http://bad:8100"}, KeyError("name"))
    assert inst.verdict == Health.DOWN
    assert inst.services[0].state == Health.DOWN
    assert "KeyError" in inst.notes[0]


async def test_probe_all_isolates_a_failing_instance(monkeypatch):
    good = InstanceHealth(name="good", url="http://good:8100", verdict=Health.UP, services=[])

    async def fake_probe(instance, timeout):
        if instance["name"] == "bad":
            raise KeyError("boom")
        return good

    monkeypatch.setattr(monitor, "probe_instance", fake_probe)
    fleet = await monitor.probe_all(
        [{"name": "good", "url": "http://good:8100"}, {"name": "bad", "url": "http://bad:8100"}], 1.0
    )
    assert fleet[0].verdict == Health.UP  # healthy instance unaffected
    assert fleet[1].verdict == Health.DOWN  # failing instance degraded, not fatal
    assert "boom" in fleet[1].notes[0]


# --- CLI argument validation -------------------------------------------------


@pytest.mark.parametrize("bad", ["0", "-5"])
def test_watch_rejects_non_positive_interval(monkeypatch, capsys, bad):
    """`--watch 0` used to be falsy and silently fell through to one-shot mode."""
    monkeypatch.setattr(sys, "argv", ["monitor.py", "--watch", bad])
    monkeypatch.setattr(monitor, "load_instances", lambda *a, **k: [])
    with pytest.raises(SystemExit) as exc:
        monitor.main()
    assert exc.value.code == 2
    assert "positive interval" in capsys.readouterr().err


def test_watch_without_value_uses_default_interval(monkeypatch):
    """Bare `--watch` still means "every 5s" and reaches the watch path, not one-shot."""
    monkeypatch.setattr(sys, "argv", ["monitor.py", "--watch"])
    monkeypatch.setattr(monitor, "load_instances", lambda *a, **k: [])
    monkeypatch.setattr(monitor, "_rich_available", lambda: True)
    seen = {}

    async def fake_watch(instances, timeout, interval):
        seen["interval"] = interval

    monkeypatch.setattr(monitor, "run_watch", fake_watch)
    monitor.main()
    assert seen["interval"] == 5.0
