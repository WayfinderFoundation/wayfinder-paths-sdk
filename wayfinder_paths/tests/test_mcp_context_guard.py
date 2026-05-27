from __future__ import annotations

import json
from pathlib import Path

import pytest

from wayfinder_paths.mcp.context_guard import guard_payload


@pytest.fixture
def guard_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Guard auto-disables under pytest (via PYTEST_CURRENT_TEST). Clear it here
    so we can exercise truncation behavior, and point scratch at a temp dir."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("WAYFINDER_SCRATCH_DIR", str(tmp_path))
    yield tmp_path


def test_small_payload_passes_through(guard_enabled):
    small = {"wallets": [{"label": "a", "address": "0x1"}]}
    assert guard_payload(small, name="small") is small


def test_slim_text_passes_through(guard_enabled):
    slim = "BTC -38%\nETH -42%\nSOL -53%"
    assert guard_payload(slim, name="slim") == slim


def test_big_text_truncates(guard_enabled):
    big = "line\n" * 5_000  # ~25 KB, well above the 10 KB default
    env = guard_payload(big, name="big_text")

    assert env["_truncated"] is True
    assert env["bytes"] == len(big)
    assert env["shape"] == {
        "type": "str",
        "len": len(big),
        "lines": big.count("\n") + 1,
    }
    assert env["head"]["_str_truncated"] is True
    assert env["head"]["head"].startswith("line\n")
    assert env["head"]["tail"].endswith("line\n")

    artifact = Path(env["artifact"])
    assert artifact.exists()
    assert artifact.suffix == ".txt"
    assert artifact.read_text() == big


def test_big_dict_truncates_and_recovers(guard_enabled):
    big = {"prices": {f"ASSET_{i}": float(i) for i in range(2_000)}}
    raw_size = len(json.dumps(big))
    env = guard_payload(big, name="big_dict")

    assert env["_truncated"] is True
    assert env["bytes"] == raw_size
    assert env["shape"]["collection_sizes"]["prices"] == 2_000
    assert env["head"]["prices"]["_dict_truncated"] is True
    assert env["head"]["prices"]["len"] == 2_000
    assert len(env["head"]["prices"]["head"]) == 20  # HEAD_ITEMS

    full = json.loads(Path(env["artifact"]).read_text())
    assert len(full["prices"]) == 2_000


def test_nested_big_string_is_sliced(guard_enabled):
    payload = {
        "ok": False,
        "error": {"code": "x", "message": "y", "traceback": "Traceback...\n" * 5_000},
    }
    env = guard_payload(payload, name="err")

    assert env["_truncated"] is True
    tb = env["head"]["error"]["traceback"]
    assert isinstance(tb, dict) and tb["_str_truncated"] is True
    # Structured fields preserved verbatim
    assert env["head"]["error"]["code"] == "x"
    assert env["head"]["error"]["message"] == "y"
    assert env["head"]["ok"] is False


def test_explicit_max_bytes_overrides_env(guard_enabled):
    payload = {"x": "a" * 2_000}  # ~2 KB
    # Under default 10 KB → passes
    assert guard_payload(payload, name="ok") == payload
    # Forced 1 KB → truncates
    env = guard_payload(payload, name="forced", max_bytes=1_000)
    assert env["_truncated"] is True


@pytest.mark.asyncio
async def test_adapter_call_unfiltered_truncates(guard_enabled, monkeypatch):
    """End-to-end: a real MCP tool returning a big-ish payload spills cleanly."""
    monkeypatch.setenv("WF_MAX_CONTEXT_BYTES", "5000")

    from wayfinder_paths.mcp.tools.hyperliquid import hyperliquid_search_mid_prices

    out = await hyperliquid_search_mid_prices(asset_names=None)
    assert out["ok"] is True
    inner = out["result"]
    assert inner["_truncated"] is True
    assert inner["shape"]["collection_sizes"]["prices"] > 100
    assert Path(inner["artifact"]).exists()


def test_guard_skipped_inside_pytest():
    """PYTEST_CURRENT_TEST is set automatically by pytest for the duration of this test."""
    big = "x" * 50_000
    assert guard_payload(big, name="disabled") == big
