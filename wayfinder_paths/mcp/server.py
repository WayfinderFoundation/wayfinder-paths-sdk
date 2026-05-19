"""Wayfinder Paths MCP server (FastMCP).

Run locally (via Claude Code .mcp.json):
  poetry run python -m wayfinder_paths.mcp.server

The SDK exposes one MCP catalog. OpenCode scopes per-agent tool visibility with
permission wildcard rules in config; permissions separately gate sensitive calls.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Sequence
from typing import Literal

from mcp.server.fastmcp import FastMCP

from wayfinder_paths.mcp.tool_registry import tools_for_mcp
from wayfinder_paths.paths.heartbeat import maybe_heartbeat_installed_paths

MCPTransport = Literal["stdio", "sse", "streamable-http"]


def build_mcp(
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> FastMCP:
    server = FastMCP("wayfinder", host=host, port=port)
    for fn in tools_for_mcp():
        server.tool()(fn)
    return server


mcp = build_mcp()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--host",
        default=os.environ.get("WAYFINDER_MCP_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port",
        default=int(os.environ.get("WAYFINDER_MCP_PORT", "8000")),
        type=int,
    )
    parser.add_argument(
        "--transport",
        default=os.environ.get("WAYFINDER_MCP_TRANSPORT", "stdio"),
        choices=["stdio", "sse", "streamable-http"],
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    maybe_heartbeat_installed_paths(trigger="mcp-server")
    build_mcp(host=args.host, port=args.port).run(
        transport=args.transport,
    )


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        if "asyncio.run()" in str(exc) and asyncio.get_event_loop().is_running():
            main()
        else:
            raise
