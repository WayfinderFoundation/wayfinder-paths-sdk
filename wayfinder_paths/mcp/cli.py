"""CLI interface for Wayfinder MCP tools.

Auto-generates click subcommands from the FastMCP server's registered tools.

Usage:
  poetry run python -m wayfinder_paths.mcp.cli [TOOL] [OPTIONS]
  poetry run python -m wayfinder_paths.mcp.cli --help
  poetry run python -m wayfinder_paths.mcp.cli wallets --action list
  poetry run python -m wayfinder_paths.mcp.cli discover --kind strategies
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import click

from wayfinder_paths.mcp.server import mcp

TYPE_MAP = {"string": str, "integer": int, "number": float, "boolean": bool}


def _make_click_option(name: str, schema: dict) -> click.Option:
    """Convert a JSON schema property to a click option."""
    if schema.get("type") == "boolean":
        return click.Option(
            [f"--{name}/--no-{name}"],
            default=schema.get("default"),
            help=schema.get("description", ""),
        )

    if "enum" in schema:
        param_type = click.Choice(schema["enum"])
    elif schema.get("type") in TYPE_MAP:
        param_type = TYPE_MAP[schema["type"]]
    else:
        # array, object, anyOf — accept as string, parse later
        param_type = str

    return click.Option(
        [f"--{name}"],
        type=param_type,
        default=schema.get("default"),
        help=schema.get("description", ""),
    )


def _coerce_by_type(value: Any, typ: str) -> Any:
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


def _coerce_bool_string(value: Any) -> bool:
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value)


def _coerce_value(value: Any, schema: dict) -> Any:
    """Coerce a CLI value to match the JSON schema type."""
    if value is None or "enum" in schema:
        return value

    typ = schema.get("type")
    if typ:
        return _coerce_by_type(value, typ)

    for sub in schema.get("anyOf", []):
        sub_type = sub.get("type")
        if sub_type == "null":
            continue
        if sub_type == "boolean":
            return _coerce_bool_string(value)
        return _coerce_by_type(value, sub_type)

    return value


def _echo_json(data: Any) -> None:
    """Pretty-print data as JSON."""
    click.echo(json.dumps(data, indent=2, default=str))


def _echo_item(item: Any) -> None:
    """Output a single result item, formatting JSON when possible."""
    text = getattr(item, "text", None)
    if text:
        try:
            _echo_json(json.loads(text))
        except (json.JSONDecodeError, ValueError):
            click.echo(text)
    elif isinstance(item, dict):
        _echo_json(item)
    else:
        click.echo(str(item))


def _echo_result(result: Any) -> None:
    """Output tool result in appropriate format."""
    # call_tool returns (content_blocks, dict) tuple
    if isinstance(result, tuple) and len(result) == 2:
        _, data = result
        if isinstance(data, dict):
            _echo_json(data)
            return

    items = result if isinstance(result, (list, tuple)) else [result]
    for item in items:
        _echo_item(item)


def _build_tool_command(tool) -> click.Command:
    properties = tool.inputSchema.get("properties", {})
    required = set(tool.inputSchema.get("required", []))

    params = []
    for name, schema in properties.items():
        opt = _make_click_option(name, schema)
        opt.required = name in required and opt.default is None
        params.append(opt)

    def make_callback(tool_name: str, props: dict):
        def callback(**kwargs):
            args = {
                k: _coerce_value(v, props.get(k, {}))
                for k, v in kwargs.items()
                if v is not None
            }
            result = asyncio.run(mcp.call_tool(tool_name, args))
            _echo_result(result)

        return callback

    return click.Command(
        name=tool.name,
        params=params,
        callback=make_callback(tool.name, properties),
        help=tool.description or f"Call the {tool.name} MCP tool.",
    )


def _list_resources() -> None:
    """List all available MCP resources and templates."""
    resources = asyncio.run(mcp.list_resources())
    templates = asyncio.run(mcp.list_resource_templates())
    _echo_json(
        {
            "resources": [{"name": r.name, "uri": str(r.uri)} for r in resources],
            "templates": [
                {"name": t.name, "uriTemplate": t.uriTemplate} for t in templates
            ],
        }
    )


def _read_resource(uri: str) -> None:
    """Read and output a single MCP resource."""
    for item in asyncio.run(mcp.read_resource(uri)):
        text = getattr(item, "content", None) or getattr(item, "text", None)
        if text:
            try:
                _echo_json(json.loads(text))
            except (json.JSONDecodeError, ValueError):
                click.echo(text)
        else:
            click.echo(str(item))


def _build_resource_command() -> click.Command:
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
            _list_resources()
        else:
            _read_resource(uri)

    return resource_cmd


def build_cli() -> click.Group:
    group = click.Group(
        name="wayfinder",
        help="Wayfinder Paths MCP tools CLI.",
    )

    tools = asyncio.run(mcp.list_tools())
    for tool in tools:
        cmd = _build_tool_command(tool)
        group.add_command(cmd)

    group.add_command(_build_resource_command())

    return group


def main():
    cli = build_cli()
    cli(standalone_mode=True)


if __name__ == "__main__":
    main()
