"""The MCP server runs every tool call as a concurrent task on ONE event loop
(lowlevel Server dispatches each request via `tg.start_soon`). So a tool handler
that makes a *blocking* call freezes the whole loop — every other in-flight
request (including a freshly-restarted OpenCode completing its connect handshake)
stalls until the blocking call returns. A handler that *awaits* its I/O yields
the loop, so other requests keep flowing.

These tests stand up a real FastMCP server in-process, launch a long-running
"job" tool that talks to a deliberately slow OpenCode endpoint, and assert a
concurrent `ping` stays responsive. The async OpenCodeClient must keep the loop
preemptible; the old synchronous client froze it (kept here as a regression
guard so the fix can't silently regress).
"""

from __future__ import annotations

import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

from wayfinder_paths.core.clients.OpenCodeClient import OpenCodeClient

# How long the mock OpenCode takes to answer /global/health. Long enough that a
# frozen loop is unmistakable, short enough to keep the test fast.
SLOW_HEALTH_SECONDS = 1.5


class _SlowHealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        time.sleep(SLOW_HEALTH_SECONDS)
        body = b'{"healthy": true, "version": "test"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:  # silence per-request logging
        pass


@pytest.fixture
def slow_opencode_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowHealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


def _build_server(base_url: str, *, blocking: bool) -> FastMCP:
    mcp = FastMCP("event-loop-test")
    client = OpenCodeClient(base_url)

    @mcp.tool()
    async def slow_job() -> str:
        """Simulates a scheduled job whose tool call hits a slow OpenCode."""
        if blocking:
            # Regression guard: a synchronous call inside an async handler pins
            # the single event loop. This is the pattern the async client fixes.
            with httpx.Client(timeout=httpx.Timeout(10)) as sync_client:
                sync_client.get(f"{base_url}/global/health")
        else:
            await client.healthy()  # awaited → yields the loop
        return "done"

    @mcp.tool()
    async def ping() -> str:
        """Stands in for OpenCode's boot handshake needing a quick answer."""
        return "pong"

    return mcp


async def _pings_completed_during_job(mcp: FastMCP) -> int:
    """Count how many `ping` tool calls complete while `slow_job` is in flight.

    Counting (not latency) is immune to task-scheduling order: a frozen loop
    can't advance a sleep or dispatch a ping, so a blocking handler yields ~0
    pings, while an awaiting handler lets the loop service hundreds.
    """
    async with create_connected_server_and_client_session(mcp) as session:
        job = asyncio.create_task(session.call_tool("slow_job", {}))
        pings = 0
        while not job.done():
            await session.call_tool("ping", {})
            pings += 1
        await job
        return pings


async def test_async_client_keeps_event_loop_preemptible(slow_opencode_url):
    mcp = _build_server(slow_opencode_url, blocking=False)
    pings = await _pings_completed_during_job(mcp)
    # The loop serviced many pings while slow_job awaited a 1.5s health check —
    # it was never frozen.
    assert pings > 20, (
        f"only {pings} pings completed while an async job ran — the event loop "
        "was starved despite the async client"
    )


async def test_blocking_client_freezes_event_loop_regression(slow_opencode_url):
    # Guard: prove the harness actually detects a frozen loop. A synchronous
    # call inside the handler pins the loop, so essentially no pings get through.
    mcp = _build_server(slow_opencode_url, blocking=True)
    pings = await _pings_completed_during_job(mcp)
    assert pings <= 3, (
        f"{pings} pings completed during a blocking job — expected ~0; the test "
        "can no longer distinguish a frozen loop from a preemptible one"
    )
