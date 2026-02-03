from __future__ import annotations

import pytest

from wayfinder_paths.mcp.utils import parse_amount_to_raw, repo_root, sha256_json


def test_repo_root_finds_pyproject():
    root = repo_root()
    assert (root / "pyproject.toml").exists()


def test_parse_amount_to_raw_scales_and_floors():
    assert parse_amount_to_raw("1", 6) == 1_000_000
    # Flooring: 1.23456789 with 6 decimals -> 1_234_567
    assert parse_amount_to_raw("1.23456789", 6) == 1_234_567


def test_parse_amount_to_raw_rejects_non_positive():
    with pytest.raises(ValueError, match="positive"):
        parse_amount_to_raw("0", 18)
    with pytest.raises(ValueError, match="positive"):
        parse_amount_to_raw("-1", 18)


def test_parse_amount_to_raw_rejects_too_small_after_scaling():
    with pytest.raises(ValueError, match="too small"):
        parse_amount_to_raw("0.0000001", 6)


def test_sha256_json_is_stable_for_key_order():
    a = sha256_json({"b": 2, "a": 1})
    b = sha256_json({"a": 1, "b": 2})
    assert a == b
    assert a.startswith("sha256:")
