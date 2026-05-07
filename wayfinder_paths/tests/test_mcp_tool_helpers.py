from __future__ import annotations

import pytest

from wayfinder_paths.mcp.utils import (
    catch_errors,
    throw_if_empty_str,
    throw_if_none,
    throw_if_not_number,
)


def test_throw_if_none_raises_with_exact_message():
    with pytest.raises(ValueError, match="^helpful x message$"):
        throw_if_none("helpful x message", None)


@pytest.mark.parametrize("value", [0, False, ""])
def test_throw_if_none_passes_for_falsy_non_none(value):
    throw_if_none("msg", value)


@pytest.mark.parametrize(
    "value,expected",
    [(1, 1.0), ("1.5", 1.5), ("-3", -3.0)],
)
def test_throw_if_not_number_returns_float(value, expected):
    assert throw_if_not_number("msg", value) == expected


@pytest.mark.parametrize("value", [None, "abc", [], {}])
def test_throw_if_not_number_raises_with_exact_message(value):
    with pytest.raises(ValueError, match="^x must be a number$"):
        throw_if_not_number("x must be a number", value)


def test_throw_if_empty_str_returns_stripped():
    assert throw_if_empty_str("msg", "  ok  ") == "ok"


@pytest.mark.parametrize("value", [None, "", "   ", 0, []])
def test_throw_if_empty_str_raises_with_exact_message(value):
    with pytest.raises(ValueError, match="^x is required for foo$"):
        throw_if_empty_str("x is required for foo", value)


@pytest.mark.asyncio
async def test_catch_errors_async_value_error():
    @catch_errors
    async def fn():
        raise ValueError("x is required")

    assert await fn() == {
        "ok": False,
        "error": {"code": "error", "message": "x is required", "details": None},
    }


@pytest.mark.asyncio
async def test_catch_errors_async_generic_error():
    @catch_errors
    async def fn():
        raise Exception("boom")

    assert await fn() == {
        "ok": False,
        "error": {"code": "error", "message": "boom", "details": None},
    }


def test_catch_errors_sync_value_error():
    @catch_errors
    def fn():
        raise ValueError("x is required")

    assert fn() == {
        "ok": False,
        "error": {"code": "error", "message": "x is required", "details": None},
    }


def test_catch_errors_sync_generic_error():
    @catch_errors
    def fn():
        raise Exception("boom")

    assert fn() == {
        "ok": False,
        "error": {"code": "error", "message": "boom", "details": None},
    }
