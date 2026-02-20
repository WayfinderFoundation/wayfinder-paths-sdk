from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.mcp.tools.adapter_execute import adapter_execute


@pytest.mark.asyncio
async def test_adapter_execute_rejects_non_allowlisted_method(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WAYFINDER_RUNS_DIR", str(tmp_path / "runs"))

    wallet = {
        "address": "0x000000000000000000000000000000000000dEaD",
        "private_key_hex": "0x" + "11" * 32,
    }

    with patch(
        "wayfinder_paths.mcp.tools.adapter_execute.find_wallet_by_label",
        return_value=wallet,
    ):
        out = await adapter_execute(
            adapter="avantis_adapter",
            method="nope",
            wallet_label="main",
        )

    assert out["ok"] is False
    assert out["error"]["code"] == "not_allowed"


@pytest.mark.asyncio
async def test_adapter_execute_calls_allowlisted_method(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WAYFINDER_RUNS_DIR", str(tmp_path / "runs"))

    wallet = {
        "address": "0x000000000000000000000000000000000000dEaD",
        "private_key_hex": "0x" + "11" * 32,
    }

    with (
        patch(
            "wayfinder_paths.mcp.tools.adapter_execute.find_wallet_by_label",
            return_value=wallet,
        ),
        patch(
            "wayfinder_paths.adapters.avantis_adapter.adapter.AvantisAdapter.deposit",
            new=AsyncMock(return_value=(True, "0xabc")),
        ) as dep,
    ):
        out = await adapter_execute(
            adapter="avantis_adapter",
            method="deposit",
            wallet_label="main",
            kwargs={"amount": 123},
        )

    assert dep.await_count == 1
    assert out["ok"] is True
    result = out["result"]
    assert result["adapter"] == "avantis_adapter"
    assert result["method"] == "deposit"
    assert result["success"] is True
    assert result["output"] == "0xabc"


@pytest.mark.asyncio
async def test_adapter_execute_forbids_sensitive_kwargs(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WAYFINDER_RUNS_DIR", str(tmp_path / "runs"))

    wallet = {
        "address": "0x000000000000000000000000000000000000dEaD",
        "private_key_hex": "0x" + "11" * 32,
    }

    with patch(
        "wayfinder_paths.mcp.tools.adapter_execute.find_wallet_by_label",
        return_value=wallet,
    ):
        out = await adapter_execute(
            adapter="avantis_adapter",
            method="deposit",
            wallet_label="main",
            kwargs={"signing_callback": "nope", "amount": 1},
        )

    assert out["ok"] is False
    assert out["error"]["code"] == "invalid_request"

