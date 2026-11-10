"""Guards on the native systemd units in deploy/native/."""

from pathlib import Path

import pytest

NATIVE = Path(__file__).resolve().parent.parent / "deploy" / "native"


@pytest.mark.parametrize("unit", ["cardigan-api.service", "cardigan-worker.service"])
def test_unit_restarts_after_clean_exit(unit):
    """Settings' "Restart Components" exits cleanly; systemd must bring the process back.

    Restart=on-failure leaves a clean exit stopped, which took cardigan02 down on
    2026-10-26 when the Settings button was pressed.
    """
    lines = (NATIVE / unit).read_text().splitlines()
    policies = [line.split("=", 1)[1].strip() for line in lines if line.startswith("Restart=")]
    assert policies == ["always"]
