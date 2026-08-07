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
