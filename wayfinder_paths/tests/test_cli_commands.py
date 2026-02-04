"""Tests for CLI command building (tools and resources)."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

from click.testing import CliRunner

from wayfinder_paths.mcp.cli_builder import (
    build_resource_command,
    build_tool_command,
)


@dataclass
class FakeTool:
    name: str
    description: str
    inputSchema: dict


def _make_mock_mcp():
    """Create a mock MCP interface for testing."""
    mock = MagicMock()
    mock.call_tool = AsyncMock(return_value=([], {"ok": True}))
    mock.list_tools = AsyncMock(return_value=[])
    mock.list_resources = AsyncMock(return_value=[])
    mock.list_resource_templates = AsyncMock(return_value=[])
    mock.read_resource = AsyncMock(return_value=[])
    return mock


# --- build_tool_command tests ---


def test_build_tool_command_creates_command_with_params():
    tool = FakeTool(
        name="test_tool",
        description="A test tool",
        inputSchema={
            "properties": {
                "name": {"type": "string", "description": "User name"},
                "count": {"type": "integer", "default": 1},
            },
            "required": ["name"],
        },
    )
    mock_mcp = _make_mock_mcp()
    cmd = build_tool_command(tool, mock_mcp)

    assert cmd.name == "test_tool"
    assert "A test tool" in cmd.help
    assert len(cmd.params) == 2

    # Check required param
    name_param = next(p for p in cmd.params if p.name == "name")
    assert name_param.required is True

    # Check optional param with default
    count_param = next(p for p in cmd.params if p.name == "count")
    assert count_param.required is False
    assert count_param.default == 1


def test_build_tool_command_invokes_mcp_call_tool():
    tool = FakeTool(
        name="mock_tool",
        description="Mock tool",
        inputSchema={
            "properties": {
                "arg": {"type": "string"},
            },
            "required": ["arg"],
        },
    )
    mock_mcp = _make_mock_mcp()
    cmd = build_tool_command(tool, mock_mcp)

    runner = CliRunner()
    result = runner.invoke(cmd, ["--arg", "test_value"])

    assert result.exit_code == 0
    mock_mcp.call_tool.assert_called_once_with("mock_tool", {"arg": "test_value"})


def test_build_tool_command_coerces_types():
    tool = FakeTool(
        name="typed_tool",
        description="Typed tool",
        inputSchema={
            "properties": {
                "num": {"type": "integer"},
                "rate": {"type": "number"},
            },
            "required": [],
        },
    )
    mock_mcp = _make_mock_mcp()
    cmd = build_tool_command(tool, mock_mcp)

    runner = CliRunner()
    result = runner.invoke(cmd, ["--num", "42", "--rate", "3.14"])

    assert result.exit_code == 0
    call_args = mock_mcp.call_tool.call_args[0][1]
    assert call_args["num"] == 42
    assert call_args["rate"] == 3.14


def test_build_tool_command_omits_none_values():
    tool = FakeTool(
        name="optional_tool",
        description="Tool with optional params",
        inputSchema={
            "properties": {
                "required_arg": {"type": "string"},
                "optional_arg": {"type": "string"},
            },
            "required": ["required_arg"],
        },
    )
    mock_mcp = _make_mock_mcp()
    cmd = build_tool_command(tool, mock_mcp)

    runner = CliRunner()
    result = runner.invoke(cmd, ["--required_arg", "value"])

    assert result.exit_code == 0
    call_args = mock_mcp.call_tool.call_args[0][1]
    assert "optional_arg" not in call_args


# --- build_resource_command tests ---


def test_build_resource_command_list_resources():
    @dataclass
    class FakeResource:
        name: str
        uri: str

    @dataclass
    class FakeTemplate:
        name: str
        uriTemplate: str

    mock_mcp = _make_mock_mcp()
    mock_mcp.list_resources = AsyncMock(
        return_value=[FakeResource("res1", "proto://res1")]
    )
    mock_mcp.list_resource_templates = AsyncMock(
        return_value=[FakeTemplate("tmpl1", "proto://tmpl/{id}")]
    )

    cmd = build_resource_command(mock_mcp)
    runner = CliRunner()
    result = runner.invoke(cmd, ["--list"])

    assert result.exit_code == 0
    assert "res1" in result.output
    assert "tmpl1" in result.output


def test_build_resource_command_read_resource_json():
    @dataclass
    class FakeContent:
        content: str

    mock_mcp = _make_mock_mcp()
    mock_mcp.read_resource = AsyncMock(
        return_value=[FakeContent(content='{"data": "test"}')]
    )

    cmd = build_resource_command(mock_mcp)
    runner = CliRunner()
    result = runner.invoke(cmd, ["proto://test"])

    assert result.exit_code == 0
    assert '"data": "test"' in result.output


def test_build_resource_command_read_resource_text():
    @dataclass
    class FakeContent:
        text: str

    mock_mcp = _make_mock_mcp()
    mock_mcp.read_resource = AsyncMock(
        return_value=[FakeContent(text="plain text content")]
    )

    cmd = build_resource_command(mock_mcp)
    runner = CliRunner()
    result = runner.invoke(cmd, ["proto://test"])

    assert result.exit_code == 0
    assert "plain text content" in result.output


def test_build_resource_command_no_args_lists_resources():
    mock_mcp = _make_mock_mcp()
    cmd = build_resource_command(mock_mcp)
    runner = CliRunner()

    result = runner.invoke(cmd, [])

    assert result.exit_code == 0
    mock_mcp.list_resources.assert_called_once()
