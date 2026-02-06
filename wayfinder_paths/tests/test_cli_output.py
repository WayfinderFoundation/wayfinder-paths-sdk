"""Tests for CLI output formatting functions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from wayfinder_paths.mcp.cli_builder import (
    echo_item,
    echo_json,
    echo_result,
)

# --- echo_json tests ---


def test_echo_json_formats_dict(capsys):
    echo_json({"key": "value"})
    out = capsys.readouterr().out
    assert '"key": "value"' in out
    # Pretty printed with indent
    assert "\n" in out


def test_echo_json_handles_non_serializable(capsys):
    echo_json({"time": datetime(2024, 1, 1)})
    out = capsys.readouterr().out
    assert "2024" in out


# --- echo_item tests ---


def test_echo_item_dict_as_json(capsys):
    echo_item({"a": 1})
    assert '"a": 1' in capsys.readouterr().out


def test_echo_item_object_with_text_json(capsys):
    @dataclass
    class FakeContent:
        text: str

    echo_item(FakeContent(text='{"parsed": true}'))
    out = capsys.readouterr().out
    assert '"parsed": true' in out


def test_echo_item_object_with_text_plain(capsys):
    @dataclass
    class FakeContent:
        text: str

    echo_item(FakeContent(text="plain text"))
    assert "plain text" in capsys.readouterr().out


def test_echo_item_fallback_str(capsys):
    echo_item(12345)
    assert "12345" in capsys.readouterr().out


# --- echo_result tests ---


def test_echo_result_tuple_result(capsys):
    # call_tool returns (content_blocks, dict) tuple
    result = ([], {"ok": True, "data": "test"})
    echo_result(result)
    out = capsys.readouterr().out
    assert '"ok": true' in out


def test_echo_result_list_result(capsys):
    @dataclass
    class FakeContent:
        text: str

    result = [FakeContent(text='{"item": 1}'), FakeContent(text='{"item": 2}')]
    echo_result(result)
    out = capsys.readouterr().out
    assert '"item": 1' in out
    assert '"item": 2' in out


def test_echo_result_single_result(capsys):
    echo_result({"single": "item"})
    assert '"single": "item"' in capsys.readouterr().out
