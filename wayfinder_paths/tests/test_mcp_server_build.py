from __future__ import annotations

import json
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
        "hyperliquid_get_candles",
        "hyperliquid_get_funding_history",
        "hyperliquid_get_state",
        "polymarket_read",
        "contracts_call",
        "sports_snapshot",
        "sports_backtest_state",
        "sports_provider",
    ):
        assert required in names, f"missing tool: {required}"


def test_mcp_tool_descriptions_stay_context_efficient() -> None:
    from wayfinder_paths.mcp.server import build_mcp

    tools = build_mcp()._tool_manager.list_tools()
    descriptions = {tool.name: tool.description or "" for tool in tools}
    oversized = {
        name: len(description)
        for name, description in descriptions.items()
        if len(description) > 700
    }

    assert not oversized, f"tool descriptions exceed 700 characters: {oversized}"
    assert sum(map(len, descriptions.values())) <= 25_000


def test_non_obvious_parameters_include_schema_guidance(monkeypatch) -> None:
    from wayfinder_paths.mcp import server as mcp_server

    monkeypatch.setattr(mcp_server, "is_opencode_instance", lambda: True)
    tools = {
        tool.name: tool for tool in mcp_server.build_mcp()._tool_manager.list_tools()
    }
    guided_parameters = {
        ("onchain_swap", "amount"): "never pass wei",
        ("sports_provider", "path_params"): "JSON object",
        ("contracts_call", "args"): "JSON array",
        ("core_runner", "cron_expr"): "5-field cron",
        ("hyperliquid_place_market_order", "asset_name"): "BTC-USDC",
        ("polymarket_place_market_order", "max_slippage_pct"): "2.0 means 2%",
        ("visual_create_chart", "series"): "visual_search_chart_series",
    }

    for (tool_name, parameter), expected in guided_parameters.items():
        schema = tools[tool_name].parameters["properties"][parameter]
        assert expected in schema.get("description", ""), (
            f"{tool_name}.{parameter} lacks actionable format guidance"
        )

    manifest = [
        {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.parameters,
        }
        for tool in tools.values()
    ]
    assert len(json.dumps(manifest, separators=(",", ":"))) <= 80_000


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
