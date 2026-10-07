from unittest.mock import patch

import pytest

from wayfinder_paths.runner.daemon import RunnerDaemon
from wayfinder_paths.runner.lifecycle import ensure_daemon_started


def test_recovery_blocks_direct_start_before_database_access(monkeypatch):
    monkeypatch.setenv("WAYFINDER_RECOVERY_HOLD", "1")
    with patch("wayfinder_paths.runner.daemon.RunnerDB") as database:
        with pytest.raises(RuntimeError, match="disabled during recovery"):
            RunnerDaemon(paths=None)
    database.assert_not_called()


def test_recovery_blocks_lazy_start_even_with_override_environment(monkeypatch):
    monkeypatch.setenv("WAYFINDER_RECOVERY_HOLD", "1")
    with patch("wayfinder_paths.runner.lifecycle.RunnerControlClient") as client:
        ok, result = ensure_daemon_started(
            paths=None,
            tick_seconds=1,
            max_workers=1,
            max_failures=1,
            default_timeout_seconds=10,
            log_level="INFO",
            env={},
        )
    assert not ok
    assert result == {"error": "recovery_hold"}
    client.assert_not_called()
