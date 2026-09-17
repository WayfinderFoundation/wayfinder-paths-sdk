from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


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
        "onchain_get_settlement_assets",
        "hyperliquid_get_candles",
        "hyperliquid_get_funding_history",
        "hyperliquid_get_state",
        "hyperliquid_get_trade_results",
        "polymarket_read",
        "contracts_call",
        "sports_snapshot",
        "sports_backtest_state",
        "sports_provider",
        "quant_pattern_match",
        "quant_pattern_match_ccxt_proxy",
    ):
        assert required in names, f"missing tool: {required}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body"),
    [
        (200, b'{"results": [{"status": "recorded"}]}'),
        (200, b"null"),
        (503, b"Unavailable"),
        (200, None),
    ],
)
async def test_mcp_server_serves_tools_after_startup_heartbeat(
    tmp_path: Path, status: int, body: bytes | None
) -> None:
    # Exercise the real entrypoint and MCP handshake, not just a process that may
    # be alive but stuck before binding. Only the heartbeat backend is a stub.
    requests: list[str] = []
    release_request = threading.Event()

    class HeartbeatHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            requests.append(self.path)
            if body is None:
                # Stall until the client has timed out and MCP is ready.
                release_request.wait(timeout=40)
                return
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body)

    state_dir = tmp_path / ".wayfinder"
    state_dir.mkdir()
    (state_dir / "paths.lock.json").write_text(
        json.dumps(
            {"paths": {"demo": {"installation_id": "test", "heartbeat_token": "test"}}}
        )
    )
    with ThreadingHTTPServer(("127.0.0.1", 0), HeartbeatHandler) as backend:
        thread = threading.Thread(
            target=backend.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "wayfinder_paths.mcp.server"],
            cwd=tmp_path,
            env={
                "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
                "WAYFINDER_CONFIG_PATH": str(tmp_path / "config.json"),
                "WAYFINDER_PATHS_API_URL": f"http://127.0.0.1:{backend.server_port}",
                "OPENCODE_INSTANCE_ID": "test-instance",
                "HOME": str(tmp_path),
            },
        )
        try:
            async with asyncio.timeout(30), stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    names = {tool.name for tool in result.tools}
                    assert {
                        "core_get_wallets",
                        "onchain_swap",
                        "visual_add_workspace_chart_overlay",
                    } <= names
        finally:
            release_request.set()
            backend.shutdown()
            thread.join(timeout=2)
    assert requests == ["/api/v1/paths/installations/heartbeat-batch/"]
    assert (state_dir / "paths-heartbeat.json").exists() == (
        status == 200 and body is not None and b"recorded" in body
    )
