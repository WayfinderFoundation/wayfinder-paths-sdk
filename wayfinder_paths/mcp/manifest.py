"""Dump the wayfinder MCP tool manifest as JSON to stdout.

Run at Docker build time to bake the manifest the Rust frontend serves on
tools/list. The shape mirrors FastMCP's wire format so opencode sees the
same catalog whether the frontend is Python or Rust.
"""

from __future__ import annotations

import json
import os
import sys


def main() -> None:
    # Ensure read_resource registers (gated on this env var in
    # wayfinder_paths.mcp.server). Default to a placeholder so the manifest
    # baked into the image always has the full 21-tool catalog.
    os.environ.setdefault("OPENCODE_INSTANCE_ID", "manifest-build")
    from wayfinder_paths.mcp.server import mcp  # noqa: E402

    tools = []
    for tool in mcp._tool_manager.list_tools():
        tools.append(
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.parameters,
            }
        )
    json.dump({"tools": tools}, sys.stdout, indent=2)


if __name__ == "__main__":
    main()
