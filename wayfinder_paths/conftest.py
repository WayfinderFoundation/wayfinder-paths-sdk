import shutil
import sys
from collections import namedtuple
from pathlib import Path

import pytest

pytest_plugins = ["wayfinder_paths.testing.gorlami"]

# Add repo root to path so tests.test_utils can be imported
_repo_root = Path(__file__).parent.parent
_repo_root_str = str(_repo_root)

_DiskUsage = namedtuple("_DiskUsage", "total used free")
_GB = 1024**3


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
