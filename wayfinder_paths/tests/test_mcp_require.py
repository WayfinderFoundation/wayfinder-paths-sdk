from __future__ import annotations

from wayfinder_paths.mcp.utils import (
    nonempty_str,
    not_none,
    positive_number,
    require,
)


def test_not_none_predicate():
    assert not_none("x") is True
    assert not_none(0) is True
    assert not_none("") is True
    assert not_none(None) is False


def test_nonempty_str_predicate():
    assert nonempty_str("ok") is True
    assert nonempty_str("  hi  ") is True
    assert nonempty_str("") is False
    assert nonempty_str("   ") is False
    assert nonempty_str(None) is False
    assert nonempty_str(0) is False


def test_positive_number_predicate():
    assert positive_number(1) is True
    assert positive_number(1.5) is True
    assert positive_number(0) is False
    assert positive_number(-1) is False
    assert positive_number(-0.5) is False
    assert positive_number(True) is False
    assert positive_number(False) is False
    assert positive_number(None) is False
    assert positive_number("1") is False


def test_require_single_failed_returns_err():
    result = require([("amount_usdc", None, not_none)])
    assert result == {
        "ok": False,
        "error": {
            "code": "invalid_request",
            "message": "amount_usdc is required",
            "details": None,
        },
    }


def test_require_multiple_failed_uses_plural_verb():
    result = require(
        [
            ("amount_usdc", None, not_none),
            ("market_slug", None, not_none),
        ]
    )
    assert result is not None
    assert result["error"]["message"] == "amount_usdc, market_slug are required"


def test_require_singular_uses_is():
    result = require([("price", None, not_none)])
    assert result is not None
    assert result["error"]["message"] == "price is required"


def test_require_mixed_skips_passing_checks():
    result = require(
        [
            ("amount_usdc", 10, not_none),
            ("market_slug", None, not_none),
            ("token_id", None, not_none),
        ]
    )
    assert result is not None
    assert result["error"]["message"] == "market_slug, token_id are required"


def test_require_all_pass_returns_none():
    result = require(
        [
            ("amount_usdc", 10, not_none),
            ("market_slug", "abc", not_none),
        ]
    )
    assert result is None


def test_require_empty_list_returns_none():
    assert require([]) is None


def test_require_with_nonempty_str_predicate():
    assert require([("label", "main", nonempty_str)]) is None
    assert require([("label", "  ", nonempty_str)]) is not None
    assert require([("label", "", nonempty_str)]) is not None


def test_require_with_positive_number_predicate():
    assert require([("amount", 1.5, positive_number)]) is None
    assert require([("amount", 0, positive_number)]) is not None
    assert require([("amount", True, positive_number)]) is not None


def test_require_with_custom_lambda():
    is_even = lambda v: isinstance(v, int) and v % 2 == 0
    assert require([("count", 4, is_even)]) is None
    result = require([("count", 3, is_even)])
    assert result is not None
    assert result["error"]["message"] == "count is required"
