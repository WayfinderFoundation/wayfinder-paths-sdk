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
        err: dict = {
            "code": -32000,
            "message": f"{type(exc).__name__}: {exc}",
        }
        # Tracebacks across the MCP boundary (worker → frontend → opencode →
        # agent context) are noisy on routine tool errors. Include only when
        # explicitly debugging.
        if os.environ.get("WAYFINDER_MCP_DEBUG"):
            err["traceback"] = traceback.format_exc()
        payload = {"id": rid, "error": err}
    line = (json.dumps(payload, default=str) + "\n").encode("utf-8")
    writer.write(line)
    try:
        await writer.drain()
    except ConnectionResetError:
        pass


async def run(socket_path: str) -> None:
    """Run the worker on the given Unix socket. Spawned by the Rust frontend
    (`wayfinder-mcp --worker-script wayfinder_paths/mcp/worker.py`); also
    runnable manually as `python -m wayfinder_paths.mcp.worker --socket ...`."""
    if os.path.exists(socket_path):
        os.unlink(socket_path)
    server = await asyncio.start_unix_server(_handle_client, path=socket_path)
    os.chmod(socket_path, 0o600)
    tool_count = len(mcp._tool_manager.list_tools())
    print(
        f"wayfinder-mcp-worker ready socket={socket_path} tools={tool_count}",
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
