from __future__ import annotations

import subprocess
import sys


def test_adapter_import_does_not_trigger_client_cycle() -> None:
    script = "\n".join(
        (
            "from wayfinder_paths.mcp.scripting import get_adapter",
            "from wayfinder_paths.adapters.hyperliquid_adapter import HyperliquidAdapter",
            "assert get_adapter is not None",
            "assert HyperliquidAdapter is not None",
        )
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_client_package_preserves_public_exports() -> None:
    from wayfinder_paths.core.clients import (
        DELTA_LAB_CLIENT,
        POOL_CLIENT,
        RESEARCH_CLIENT,
        TOKEN_CLIENT,
        DeltaLabClient,
        ResearchClient,
    )

    assert isinstance(DELTA_LAB_CLIENT, DeltaLabClient)
    assert isinstance(RESEARCH_CLIENT, ResearchClient)
    assert POOL_CLIENT is not None
    assert TOKEN_CLIENT is not None
