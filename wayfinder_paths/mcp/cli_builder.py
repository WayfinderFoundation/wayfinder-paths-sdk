"""CLI builder utilities for generating click commands from MCP tools.

This module contains the pure logic for building CLI commands from MCP tool
definitions. It has no dependencies on the actual MCP server, making it
easy to test in isolation.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Protocol

import click

TYPE_MAP = {"string": str, "integer": int, "number": float, "boolean": bool}


class MCPInterface(Protocol):
    """Protocol for MCP server interface used by CLI."""

    async def call_tool(self, name: str, args: dict) -> Any: ...
    async def list_tools(self) -> list: ...
    async def list_resources(self) -> list: ...
    async def list_resource_templates(self) -> list: ...
    async def read_resource(self, uri: str) -> list: ...


def make_click_option(
    name: str, schema: dict, *, required: bool = False
) -> click.Option:
    """Convert a JSON schema property to a click option."""
    has_default = "default" in schema
    default = schema.get("default")
    # required only applies when there's no default
    is_required = required and not has_default

    # Build kwargs, only include default if explicitly specified
    kwargs: dict = {"help": schema.get("description", ""), "required": is_required}
    if has_default:
        kwargs["default"] = default

    if schema.get("type") == "boolean":
        return click.Option([f"--{name}/--no-{name}"], **kwargs)

    if "enum" in schema:
        kwargs["type"] = click.Choice(schema["enum"])
    elif schema.get("type") in TYPE_MAP:
        kwargs["type"] = TYPE_MAP[schema["type"]]
    else:
        # array, object, anyOf — accept as string, parse later
        kwargs["type"] = str

    return click.Option([f"--{name}"], **kwargs)


def coerce_by_type(value: Any, typ: str | None) -> Any:
    """Coerce a value to the specified JSON schema type."""
    if typ in ("boolean", "string") or typ is None:
        return value
    if typ == "integer":
        return int(value)
    if typ == "number":
        return float(value)
    if typ in ("array", "object"):
        return json.loads(value) if isinstance(value, str) else value
    return value


def coerce_bool_string(value: Any) -> bool:
    """Coerce a string value to boolean."""
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value)


def coerce_value(value: Any, schema: dict) -> Any:
    """Coerce a CLI value to match the JSON schema type."""
    if value is None or "enum" in schema:
        return value

    typ = schema.get("type")
    if typ:
        return coerce_by_type(value, typ)

    for sub in schema.get("anyOf", []):
        sub_type = sub.get("type")
        if sub_type == "null":
            continue
        if sub_type == "boolean":
            return coerce_bool_string(value)
        return coerce_by_type(value, sub_type)

    return value


def echo_json(data: Any) -> None:
    """Pretty-print data as JSON."""
    click.echo(json.dumps(data, indent=2, default=str))


def echo_item(item: Any) -> None:
    """Output a single result item, formatting JSON when possible."""
    text = getattr(item, "text", None)
    if text:
        try:
            echo_json(json.loads(text))
        except (json.JSONDecodeError, ValueError):
            click.echo(text)
    elif isinstance(item, dict):
        echo_json(item)
    else:
        click.echo(str(item))


def echo_result(result: Any) -> None:
    """Output tool result in appropriate format."""
    # call_tool returns (content_blocks, dict) tuple
    if isinstance(result, tuple) and len(result) == 2:
        _, data = result
        if isinstance(data, dict):
            echo_json(data)
            return

    items = result if isinstance(result, (list, tuple)) else [result]
    for item in items:
        echo_item(item)


def build_tool_command(tool, mcp: MCPInterface) -> click.Command:
    """Build a click command for an MCP tool."""
    properties = tool.inputSchema.get("properties", {})
    required_names = set(tool.inputSchema.get("required", []))

    params = []
    for name, schema in properties.items():
        opt = make_click_option(name, schema, required=name in required_names)
        params.append(opt)

    def make_callback(tool_name: str, props: dict):
        def callback(**kwargs):
            args = {
                k: coerce_value(v, props.get(k, {}))
                for k, v in kwargs.items()
                if v is not None
            }
            result = asyncio.run(mcp.call_tool(tool_name, args))
            echo_result(result)

        return callback

    return click.Command(
        name=tool.name,
        params=params,
        callback=make_callback(tool.name, properties),
        help=tool.description or f"Call the {tool.name} MCP tool.",
    )


def list_resources(mcp: MCPInterface) -> None:
    """List all available MCP resources and templates."""
    resources = asyncio.run(mcp.list_resources())
    templates = asyncio.run(mcp.list_resource_templates())
    echo_json(
        {
            "resources": [{"name": r.name, "uri": str(r.uri)} for r in resources],
            "templates": [
                {"name": t.name, "uriTemplate": t.uriTemplate} for t in templates
            ],
        }
    )


def read_resource(uri: str, mcp: MCPInterface) -> None:
    """Read and output a single MCP resource."""
    for item in asyncio.run(mcp.read_resource(uri)):
        text = getattr(item, "content", None) or getattr(item, "text", None)
        if text:
            try:
                echo_json(json.loads(text))
            except (json.JSONDecodeError, ValueError):
                click.echo(text)
        else:
            click.echo(str(item))


def build_resource_command(mcp: MCPInterface) -> click.Command:
    """Build the resource command for listing/reading MCP resources."""

    @click.command(
        name="resource", help="Read an MCP resource by URI, or list all with --list."
    )
    @click.argument("uri", required=False)
    @click.option(
        "--list",
        "list_all",
        is_flag=True,
        help="List available resources and templates.",
    )
    def resource_cmd(uri: str | None, list_all: bool):
        if list_all or not uri:
            list_resources(mcp)
        else:
            read_resource(uri, mcp)

    return resource_cmd


def build_cli(mcp: MCPInterface) -> click.Group:
    """Build the CLI group with all MCP tools as subcommands.

    Args:
        mcp: MCP server interface.
    """
    group = click.Group(
        name="wayfinder",
        help="Wayfinder Paths MCP tools CLI.",
    )

    tools = asyncio.run(mcp.list_tools())
    for tool in tools:
        cmd = build_tool_command(tool, mcp)
        group.add_command(cmd)

    group.add_command(build_resource_command(mcp))

    return group
