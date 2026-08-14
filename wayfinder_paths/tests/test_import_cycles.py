"""A circular import only detonates for specific first-import entry points,
and one shared pytest process can never see it: whichever module conftest
pulls in first settles sys.modules for the whole run. Each module here is
imported as the very first wayfinder import in a fresh interpreter — the
standalone-script pattern that broke live (wallets-first on shells)."""

from __future__ import annotations

import subprocess
import sys

import pytest

FIRST_IMPORT_MODULES = [
    "wayfinder_paths.core.utils.wallets",
    "wayfinder_paths.core.utils.transaction",
    "wayfinder_paths.core.utils.tokens",
    "wayfinder_paths.core.utils.svm_tokens",
    "wayfinder_paths.core.clients",
    "wayfinder_paths.mcp.scripting",
]


@pytest.mark.parametrize("module", FIRST_IMPORT_MODULES)
def test_first_import_resolves(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"first import of {module} failed:\n{result.stderr}"
