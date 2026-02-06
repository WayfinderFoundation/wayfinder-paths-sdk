"""Integration tests for the CLI with mocked MCP."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import click
from click.testing import CliRunner

from wayfinder_paths.mcp.cli_builder import build_cli


@dataclass
class FakeTool:
    name: str
    description: str
    inputSchema: dict


def _make_mock_mcp(tools: list[FakeTool] | None = None):
    """Create a mock MCP interface for testing."""
    mock = MagicMock()
    mock.call_tool = AsyncMock(return_value=([], {"ok": True}))
    mock.list_tools = AsyncMock(return_value=tools or [])
    mock.list_resources = AsyncMock(return_value=[])
    mock.list_resource_templates = AsyncMock(return_value=[])
    mock.read_resource = AsyncMock(return_value=[])
    return mock


# --- build_cli tests ---


def test_build_cli_builds_group_with_tools():
    fake_tools = [
        FakeTool(
            name="tool_a",
            description="Tool A",
            inputSchema={"properties": {}, "required": []},
        ),
        FakeTool(
            name="tool_b",
            description="Tool B",
            inputSchema={"properties": {}, "required": []},
        ),
    ]
    mock_mcp = _make_mock_mcp(fake_tools)

    cli = build_cli(mock_mcp)

    assert isinstance(cli, click.Group)
    assert "tool_a" in cli.commands
    assert "tool_b" in cli.commands
    assert "resource" in cli.commands


def test_build_cli_help():
    fake_tools = [
        FakeTool(
            name="example",
            description="Example tool",
            inputSchema={"properties": {}, "required": []},
        ),
    ]
    mock_mcp = _make_mock_mcp(fake_tools)

    cli = build_cli(mock_mcp)
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "example" in result.output
    assert "resource" in result.output


# --- Boolean flag tests ---


def test_cli_tool_with_boolean_flag():
    tool = FakeTool(
        name="flagged_tool",
        description="Tool with boolean flag",
        inputSchema={
            "properties": {
                "verbose": {"type": "boolean", "default": False},
            },
            "required": [],
        },
    )
    mock_mcp = _make_mock_mcp([tool])

    cli = build_cli(mock_mcp)
    runner = CliRunner()

    # Test with --verbose
    result = runner.invoke(cli, ["flagged_tool", "--verbose"])
    assert result.exit_code == 0
    assert mock_mcp.call_tool.call_args[0][1]["verbose"] is True

    # Test with --no-verbose
    mock_mcp.call_tool.reset_mock()
    result = runner.invoke(cli, ["flagged_tool", "--no-verbose"])
    assert result.exit_code == 0
    assert mock_mcp.call_tool.call_args[0][1]["verbose"] is False


# --- Enum param tests ---


def test_cli_tool_with_enum():
    tool = FakeTool(
        name="enum_tool",
        description="Tool with enum",
        inputSchema={
            "properties": {
                "action": {"enum": ["start", "stop", "restart"]},
            },
            "required": ["action"],
        },
    )
    mock_mcp = _make_mock_mcp([tool])

    cli = build_cli(mock_mcp)
    runner = CliRunner()

    result = runner.invoke(cli, ["enum_tool", "--action", "start"])
    assert result.exit_code == 0
    assert mock_mcp.call_tool.call_args[0][1]["action"] == "start"


def test_cli_tool_with_enum_invalid_value():
    tool = FakeTool(
        name="enum_tool",
        description="Tool with enum",
        inputSchema={
            "properties": {
                "action": {"enum": ["start", "stop", "restart"]},
            },
            "required": ["action"],
        },
    )
    mock_mcp = _make_mock_mcp([tool])

    cli = build_cli(mock_mcp)
    runner = CliRunner()

    # Invalid enum value should fail
    result = runner.invoke(cli, ["enum_tool", "--action", "invalid"])
    assert result.exit_code != 0


# --- Array param tests ---


def test_cli_tool_with_json_array():
    tool = FakeTool(
        name="array_tool",
        description="Tool with array param",
        inputSchema={
            "properties": {
                "items": {"type": "array"},
            },
            "required": ["items"],
        },
    )
    mock_mcp = _make_mock_mcp([tool])

    cli = build_cli(mock_mcp)
    runner = CliRunner()

    result = runner.invoke(cli, ["array_tool", "--items", '["a", "b", "c"]'])
    assert result.exit_code == 0
    assert mock_mcp.call_tool.call_args[0][1]["items"] == ["a", "b", "c"]


# --- Required param tests ---


def test_cli_missing_required_param():
    tool = FakeTool(
        name="required_tool",
        description="Tool with required param",
        inputSchema={
            "properties": {
                "name": {"type": "string"},
            },
            "required": ["name"],
        },
    )
    mock_mcp = _make_mock_mcp([tool])

    cli = build_cli(mock_mcp)
    runner = CliRunner()

    result = runner.invoke(cli, ["required_tool"])
    assert result.exit_code != 0
    # Click reports missing required options
    assert "Missing option" in result.output or "--name" in result.output
