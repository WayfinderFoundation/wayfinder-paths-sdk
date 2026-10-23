import inspect
import json
import re
import sys
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts.eval_execution import (
    CASES,
    RECIPIENT,
    ROOT,
    check_trace,
    fixture_server,
    prepare_workspace,
)
from wayfinder_paths.core.utils import web3
from wayfinder_paths.mcp.tools.execute import onchain_send, onchain_swap
from wayfinder_paths.mcp.tools.quotes import onchain_quote_swap
from wayfinder_paths.mcp.tools.run_script import core_run_script
from wayfinder_paths.mcp.tools.wallets import core_get_wallets

SCENARIOS = json.loads(CASES.read_text())


@pytest.mark.asyncio
async def test_documented_web3_and_token_read_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Execute the actual skill example, with only the RPC boundary mocked."""
    skill = (ROOT / ".claude/skills/writing-wayfinder-scripts/SKILL.md").read_text()
    example = next(
        block
        for block in re.findall(r"```python\n(.*?)```", skill, re.S)
        if "async with web3_from_chain_id(8453)" in block
    )
    node = MagicMock()
    node.eth.get_balance = AsyncMock(return_value=123)
    node.eth.contract.return_value.functions.balanceOf.return_value.call = AsyncMock(
        return_value=456
    )
    node.provider.disconnect = AsyncMock()
    monkeypatch.setattr(web3, "get_web3s_from_chain_id", lambda _: [node])
    namespace: dict[str, Any] = {"addr": "0x" + "11" * 20, "token": "0x" + "22" * 20}
    exec(
        "async def example():\n"
        + textwrap.indent(example, "    ")
        + "    return balance, token_balance\n",
        namespace,
    )
    assert await namespace["example"]() == (123, 456)
    node.provider.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_script_runner_uses_current_interpreter_and_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WAYFINDER_RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(
        "wayfinder_paths.mcp.utils._report_tool_metric", lambda *args: None
    )
    script = tmp_path / "interpreter.py"
    script.write_text(
        "import json, os, sys, httpx\nprint(json.dumps([sys.executable, os.getcwd()]))\n"
    )
    output = await core_run_script(script_path=str(script))
    assert output["ok"] is True
    assert output["result"]["exit_code"] == 0
    assert json.loads(output["result"]["stdout"]) == [sys.executable, str(ROOT)]


@pytest.mark.asyncio
@pytest.mark.parametrize("bridge", [False, True])
async def test_recovery_examples_only_read_original_transaction(
    monkeypatch: pytest.MonkeyPatch,
    bridge: bool,
) -> None:
    skill = (ROOT / ".claude/skills/writing-wayfinder-scripts/SKILL.md").read_text()
    marker = (
        "BRAP_CLIENT.wait_for_bridge_execution"
        if bridge
        else "receipt = await wait_for_transaction_receipt"
    )
    example = next(
        block
        for block in re.findall(r"```python\n(.*?)```", skill, re.S)
        if marker in block
    )
    target = (
        "wayfinder_paths.core.clients.BRAPClient.BRAP_CLIENT.wait_for_bridge_execution"
        if bridge
        else "wayfinder_paths.core.utils.transaction.wait_for_transaction_receipt"
    )
    read = AsyncMock(return_value={"is_success": False} if bridge else {"status": 1})
    monkeypatch.setattr(target, read)
    namespace: dict[str, Any] = {
        "chain_id": 8453,
        "txn_hash": "0x" + "aa" * 32,
        "bridge_tracking": {"provider": "lifi"},
    }
    exec("async def example():\n" + textwrap.indent(example, "    "), namespace)
    await namespace["example"]()
    read.assert_awaited_once()
    assert read.await_args is not None
    kwargs = read.await_args.kwargs
    assert kwargs["tx_hash" if bridge else "txn_hash"] == namespace["txn_hash"]
    assert kwargs["timeout_seconds" if bridge else "timeout"] == 15


@pytest.mark.asyncio
async def test_fixture_tools_match_real_parameter_names(tmp_path: Path) -> None:
    server = fixture_server(SCENARIOS[0], tmp_path / "trace")
    originals = {
        fn.__name__: fn
        for fn in (
            core_get_wallets,
            core_run_script,
            onchain_send,
            onchain_swap,
            onchain_quote_swap,
        )
    }
    for tool in await server.list_tools():
        assert set(tool.inputSchema["properties"]) == set(
            inspect.signature(originals[tool.name]).parameters
        )


def test_eval_environment_does_not_inherit_production_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WAYFINDER_CONFIG_PATH", "/production/config.json")
    monkeypatch.setenv("OPENCODE_CONFIG", "/production/opencode.json")
    monkeypatch.setenv("FLY_API_TOKEN", "must-not-leak")
    env = prepare_workspace(
        tmp_path,
        SCENARIOS[0],
        tmp_path / "trace",
        "wayfinder/deepseek-v4-pro",
        "https://llm-dev.wayfinder.ai/v1",
    )
    assert (
        not {"WAYFINDER_CONFIG_PATH", "OPENCODE_CONFIG", "FLY_API_TOKEN"} & env.keys()
    )
    config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert list(config["mcp"]) == ["wayfinder"]
    assert config["mcp"]["wayfinder"]["type"] == "local"
    assert "scripts.eval_execution" in config["mcp"]["wayfinder"]["command"]
    permissions = config["agent"]["wayfinder"]["permission"]
    assert permissions["*"] == "deny"
    assert not {"bash", "webfetch", "task", "websearch"} & permissions.keys()
    assert not (tmp_path / "config.json").exists()
    assert config["mcp"]["wayfinder"]["environment"]["WAYFINDER_API_KEY"] == ""


@pytest.mark.parametrize("case", SCENARIOS, ids=lambda case: case["id"])
def test_trace_gate_rejects_duplicate_writes(case: dict[str, Any]) -> None:
    calls = [{"tool": "swap", "arguments": {"amount": "2.0"}}] * 2
    assert "Unnecessary new swap/quote" in check_trace(case, calls)


@pytest.mark.parametrize("amount", ["0.001", "0.0038"])
def test_trace_gate_preserves_approved_amount(amount: str) -> None:
    case = next(case for case in SCENARIOS if case["id"] == "extra_funds")
    calls = [
        {"tool": "wallets", "arguments": {}},
        {
            "tool": "send",
            "arguments": {
                "amount": amount,
                "recipient": RECIPIENT,
                "wallet_label": "main",
                "token": "native",
                "chain_id": 42161,
            },
        },
    ]
    assert bool(check_trace(case, calls)) is (amount != "0.001")


@pytest.mark.asyncio
async def test_fixture_script_never_executes_supplied_file(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    script = tmp_path / "script.py"
    script.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    server = fixture_server(SCENARIOS[-1], tmp_path / "trace")
    await server.call_tool("core_run_script", {"script_path": str(script)})
    assert not marker.exists()


@pytest.mark.parametrize("inspect_first", [False, True])
def test_script_repair_requires_source_inspection_before_execution(
    inspect_first: bool,
) -> None:
    read = {
        "type": "tool_use",
        "part": {
            "tool": "read",
            "state": {
                "status": "completed",
                "input": {"filePath": "/fixture/wayfinder_paths/core/utils/tokens.py"},
            },
        },
    }
    run = {"type": "tool_use", "part": {"tool": "wayfinder_core_run_script"}}
    calls = [
        {
            "tool": "script",
            "arguments": {"script_path": ".wayfinder_runs/read_balance.py"},
        }
    ]
    events = [read, run] if inspect_first else [run, read]
    assert bool(check_trace(SCENARIOS[-1], calls, events)) is (not inspect_first)
