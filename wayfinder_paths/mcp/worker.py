"""Wayfinder MCP Python worker.

Loads the wayfinder SDK MCP server once, then services tools/call requests
from the Rust frontend over a Unix socket. Line-delimited JSON protocol:

  request:  {"id": str, "name": str, "arguments": dict}
  response: {"id": str, "result": any}  OR  {"id": str, "error": {"code": int, "message": str}}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import traceback

from mcp.server.fastmcp.exceptions import ToolError

from wayfinder_paths.mcp.server import mcp


def _list_tool_names() -> list[str]:
    return [t.name for t in mcp._tool_manager.list_tools()]


async def _dispatch(name: str, arguments: dict) -> dict:
    tool = mcp._tool_manager.get_tool(name)
    if tool is None:
        raise ToolError(f"Unknown tool: {name}")
    return await tool.run(arguments or {}, convert_result=False)


async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while True:
        line = await reader.readline()
        if not line:
            break
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid = req.get("id")
        name = req.get("name")
        args = req.get("arguments") or {}
        asyncio.create_task(_serve_one(writer, rid, name, args))


async def _serve_one(writer: asyncio.StreamWriter, rid: str, name: str, args: dict) -> None:
    try:
        result = await _dispatch(name, args)
        payload = {"id": rid, "result": result}
    except Exception as exc:
        payload = {
            "id": rid,
            "error": {
                "code": -32000,
                "message": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            },
        }
    line = (json.dumps(payload, default=str) + "\n").encode("utf-8")
    writer.write(line)
    try:
        await writer.drain()
    except ConnectionResetError:
        pass


async def run(socket_path: str) -> None:
    """Run the worker on the given Unix socket. Public entry; called from
    `wayfinder mcp worker` and from a manual `python -m wayfinder_paths.mcp.worker`."""
    if os.path.exists(socket_path):
        os.unlink(socket_path)
    server = await asyncio.start_unix_server(_handle_client, path=socket_path)
    os.chmod(socket_path, 0o600)
    print(
        f"wayfinder-mcp-worker ready socket={socket_path} tools={len(_list_tool_names())}",
        file=sys.stderr,
        flush=True,
    )
    async with server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    args = parser.parse_args()
    asyncio.run(run(args.socket))


if __name__ == "__main__":
    main()
