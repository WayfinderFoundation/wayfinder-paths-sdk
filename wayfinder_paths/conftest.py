import shutil
import sys
from collections import namedtuple
from pathlib import Path

import pytest

from wayfinder_paths.jobs import backtest_runner

pytest_plugins = ["wayfinder_paths.testing.gorlami"]

# Add repo root to path so tests.test_utils can be imported
_repo_root = Path(__file__).parent.parent
_repo_root_str = str(_repo_root)
_DEVELOPER_CONFIG = (_repo_root / "config.json").resolve()
_IGNORED_CONFIG = _repo_root / "config.json.ignored-in-tests"

_DiskUsage = namedtuple("_DiskUsage", "total used free")
_GB = 1024**3


@pytest.fixture(autouse=True)
def _paper_auto_apply_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the paper auto-apply tier for every test by default.

    Auto-apply spawns a detached apply-worker process at propose time; the
    suite's many propose fixtures are not built for that side effect (a
    green paper params proposal would silently start applying mid-test).
    Auto-apply tests opt back in with
    monkeypatch.setenv("WAYFINDER_PAPER_AUTO_APPLY", "1") and stub the
    launcher."""
    monkeypatch.setenv("WAYFINDER_PAPER_AUTO_APPLY", "0")


@pytest.fixture(autouse=True)
def _developer_runner_config_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide the developer's own backtest_runner override from every test.

    A checkout's config.json (or a shell's WAYFINDER_BACKTEST_RUNNER) may enable remote
    Sprites for real work; honoring it here would book leases from unit tests and send
    operations, campaigns and run_backtest remote. Config files a test writes into its
    own repository still load, and tests set the environment they exercise."""
    monkeypatch.delenv("WAYFINDER_BACKTEST_RUNNER", raising=False)
    resolve = backtest_runner.sdk_config_path
    monkeypatch.setattr(
        backtest_runner,
        "sdk_config_path",
        lambda root, env: (
            _IGNORED_CONFIG
            if resolve(root, env).resolve() == _DEVELOPER_CONFIG
            else resolve(root, env)
        ),
    )


@pytest.fixture(autouse=True)
def _healthy_disk_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin shutil.disk_usage to a healthy 50% volume for every test.

    The watchdog's disk-pressure standing check reads the HOST filesystem —
    on an actually-full dev box it would leak `disk_pressure` journal events
    into every unrelated watchdog-pass test. Disk-pressure tests re-patch
    explicitly and override this pin."""
    monkeypatch.setattr(
        shutil, "disk_usage", lambda path: _DiskUsage(100 * _GB, 50 * _GB, 50 * _GB)
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "smoke: mark test as a smoke test")
    config.addinivalue_line("markers", "integration: mark test as integration")
    config.addinivalue_line(
        "markers", "local: tests that hit live networks (skip in CI)"
    )
    if _repo_root_str not in sys.path:
        sys.path.insert(0, _repo_root_str)
    elif sys.path.index(_repo_root_str) > 0:
        sys.path.remove(_repo_root_str)
        sys.path.insert(0, _repo_root_str)


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "smoke" in item.nodeid:
            item.add_marker(pytest.mark.smoke)


if _repo_root_str not in sys.path:
    sys.path.insert(0, _repo_root_str)
elif sys.path.index(_repo_root_str) > 0:
    sys.path.remove(_repo_root_str)
    sys.path.insert(0, _repo_root_str)
