from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

# core_jobs is gated behind WAYFINDER_JOBS_ENABLED (default OFF). The
# quant_pattern_match* tools are prod-live on main and deliberately UNGATED —
# they import from the quant package, not jobs.
JOBS_GATED_TOOLS = {
    "core_jobs",
}
UNGATED_QUANT_TOOLS = {
    "quant_pattern_match",
    "quant_pattern_match_ccxt_proxy",
}


@pytest.fixture
def jobs_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WAYFINDER_JOBS_ENABLED", "1")


def test_build_mcp_registers_tools(jobs_enabled: None) -> None:
    from wayfinder_paths.mcp.server import build_mcp

    mcp = build_mcp()
    tools = mcp._tool_manager.list_tools()
    names = {tool.name for tool in tools}

    assert len(names) > 30, f"expected many tools to be registered, got {len(names)}"
    for required in (
        "core_get_adapters_and_strategies",
        "core_get_wallets",
        "onchain_swap",
        "onchain_get_settlement_assets",
        "hyperliquid_get_candles",
        "hyperliquid_get_funding_history",
        "hyperliquid_get_state",
        "polymarket_read",
        "contracts_call",
        "sports_snapshot",
        "sports_backtest_state",
        "sports_provider",
        "quant_pattern_match",
        "quant_pattern_match_ccxt_proxy",
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


def test_jobs_tools_absent_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # Default posture on chat-only prod boxes: WAYFINDER_JOBS_ENABLED unset →
    # core_jobs MUST NOT be registered. This is the permission-inversion guard
    # (core_run_script/core_runner are ask-gated, core_jobs was not) and the
    # pandas cold-start guard. pattern_match tools stay — prod-live on main.
    monkeypatch.delenv("WAYFINDER_JOBS_ENABLED", raising=False)

    from wayfinder_paths.mcp.server import build_mcp

    names = {tool.name for tool in build_mcp()._tool_manager.list_tools()}
    assert not (JOBS_GATED_TOOLS & names), (
        f"jobs tools leaked with flag OFF: {JOBS_GATED_TOOLS & names}"
    )
    # Non-jobs tools are still present — gating is scoped, not a full shutoff.
    assert "core_run_script" in names
    assert "onchain_swap" in names
    assert UNGATED_QUANT_TOOLS <= names, (
        f"prod-live pattern_match tools must stay registered with the flag off: {sorted(UNGATED_QUANT_TOOLS - names)}"
    )


def test_jobs_tool_module_not_imported_when_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Stronger than absence-of-registration: assert the jobs tool module (and its
    # heavy pandas import chain via the jobs package) is never even imported when
    # the flag is off. pattern_match (quant package) is exempt — it registers
    # unconditionally, matching prod main. Run in a clean subprocess so a prior
    # test that enabled jobs can't pollute sys.modules.
    monkeypatch.delenv("WAYFINDER_JOBS_ENABLED", raising=False)
    code = (
        "import sys\n"
        "from wayfinder_paths.mcp.server import build_mcp\n"
        "build_mcp()\n"
        "leaked = [m for m in ('wayfinder_paths.mcp.tools.jobs', 'wayfinder_paths.jobs.store')\n"
        "          if m in sys.modules]\n"
        "assert not leaked, f'jobs tool modules imported with flag off: {leaked}'\n"
        "print('OK')\n"
    )
    child_env = {k: v for k, v in os.environ.items() if k != "WAYFINDER_JOBS_ENABLED"}
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=child_env,
    )
    assert result.returncode == 0, (
        f"subprocess failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "OK" in result.stdout


def test_jobs_tools_present_when_flag_on(jobs_enabled: None) -> None:
    from wayfinder_paths.mcp.server import build_mcp

    names = {tool.name for tool in build_mcp()._tool_manager.list_tools()}
    assert JOBS_GATED_TOOLS <= names, (
        f"jobs tools missing with flag ON: {JOBS_GATED_TOOLS - names}"
    )
