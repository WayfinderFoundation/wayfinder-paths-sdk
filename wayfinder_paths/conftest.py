import sys
from pathlib import Path

import pytest

pytest_plugins = ["wayfinder_paths.testing.gorlami"]

# Add repo root to path so tests.test_utils can be imported
_repo_root = Path(__file__).parent.parent
_repo_root_str = str(_repo_root)


def pytest_configure(config):
    config.addinivalue_line("markers", "smoke: mark test as a smoke test")
    config.addinivalue_line("markers", "integration: mark test as integration")
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
