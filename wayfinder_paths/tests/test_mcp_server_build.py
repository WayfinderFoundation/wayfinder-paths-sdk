from __future__ import annotations

import subprocess
import sys
import time


def test_build_mcp_registers_tools() -> None:
    from wayfinder_paths.mcp.server import build_mcp

    mcp = build_mcp()
    tools = mcp._tool_manager.list_tools()
    names = {tool.name for tool in tools}

    assert len(names) > 30, f"expected many tools to be registered, got {len(names)}"
    for required in (
        "core_get_adapters_and_strategies",
        "core_get_wallets",
        "onchain_swap",
        "hyperliquid_get_state",
        "polymarket_read",
        "contracts_call",
        "sports_snapshot",
        "sports_backtest_state",
        "sports_provider",
    ):
        assert required in names, f"missing tool: {required}"


def test_mcp_server_starts_and_stays_alive() -> None:
    # `python -m wayfinder_paths.mcp.server` is the production entrypoint. Spawn it
    # and confirm it survives long enough to be serving on stdio — that proves
    # `main()` (heartbeat + build_mcp + transport boot) ran without crashing,
    # which `build_mcp()` alone won't catch.
    proc = subprocess.Popen(
        [sys.executable, "-m", "wayfinder_paths.mcp.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        time.sleep(5)
        early_exit = proc.poll()
        if early_exit is not None:
            stdout, stderr = proc.communicate(timeout=5)
            raise AssertionError(
                f"mcp server exited early with code {early_exit}\n"
                f"stdout={stdout.decode(errors='replace')}\n"
                f"stderr={stderr.decode(errors='replace')}"
            )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
