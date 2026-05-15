"""Wayfinder Paths MCP server (FastMCP).

Run locally (via Claude Code .mcp.json):
  poetry run python -m wayfinder_paths.mcp.server

The default profile is `all` for local and legacy compatibility. Wayfinder
Shells runs narrower profile servers so OpenCode agents only see the tools
needed for their role.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Sequence
from typing import Literal

from mcp.server.fastmcp import FastMCP

from wayfinder_paths.mcp.tool_registry import VALID_PROFILES, tools_for_profile
from wayfinder_paths.paths.heartbeat import maybe_heartbeat_installed_paths

MCPTransport = Literal["stdio", "sse", "streamable-http"]


def build_mcp(
    profile: str = "all",
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> FastMCP:
    server = FastMCP(f"wayfinder-{profile}", host=host, port=port)
    for entry in tools_for_profile(profile):
        server.tool()(entry.fn)
    return server


mcp = build_mcp("all")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--profile",
        default=os.environ.get("WAYFINDER_MCP_PROFILE", "all"),
        choices=sorted(VALID_PROFILES),
    )
    parser.add_argument("--host", default=os.environ.get("WAYFINDER_MCP_HOST", "127.0.0.1"))
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
    maybe_heartbeat_installed_paths(trigger=f"mcp-server:{args.profile}")
    build_mcp(args.profile, host=args.host, port=args.port).run(
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
