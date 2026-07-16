from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from wayfinder_paths.core.utils import wallets as wallets_mod
from wayfinder_paths.mcp.utils import public_wallet_view


def _ring_side(
    *, chain_type: str, address: str, expires_at: int | None = None
) -> dict[str, object]:
    side = {
        "wallet_address": address,
        "chain_type": chain_type,
        "label": "side",
        "is_active": True,
        "wallet_type": "session",
        "policy_id": "pol-1",
        "created": "2026-01-01T00:00:00Z",
    }
    if expires_at is not None:
        side["session_expires_at"] = expires_at
        side["session_expires_in"] = expires_at - 1000
    return side


def _rings() -> list[dict[str, object]]:
    return [
        {
            "label": "solar-wind",
            "evm": _ring_side(
                chain_type="ethereum",
                address="0x000000000000000000000000000000000000dEaD",
                expires_at=1784224019,
            ),
            "svm": _ring_side(
                chain_type="solana",
                address="So11111111111111111111111111111111111111112",
                expires_at=1784224999,
            ),
        },
        {
            "label": "lone-oak",
            "evm": _ring_side(
                chain_type="ethereum",
                address="0x000000000000000000000000000000000000bEEF",
            ),
            "svm": None,
        },
    ]


def _setup(monkeypatch, *, solana_enabled: bool) -> None:
    monkeypatch.setattr(wallets_mod, "get_api_key", lambda: "wk_test")
    monkeypatch.setattr(wallets_mod, "is_opencode_instance", lambda: True)
    monkeypatch.setattr(wallets_mod, "get_opencode_instance_id", lambda: "app-123")
    switches = ["solana_enabled"] if solana_enabled else []
    monkeypatch.setattr(
        wallets_mod.WALLET_CLIENT,
        "get_features",
        AsyncMock(return_value={"enabledSwitches": switches, "enabledFlags": []}),
    )
    monkeypatch.setattr(
        wallets_mod.WALLET_CLIENT,
        "list_wallet_rings",
        AsyncMock(return_value=_rings()),
    )


@pytest.mark.asyncio
async def test_load_remote_wallets_one_entry_per_leg(monkeypatch):
    _setup(monkeypatch, solana_enabled=True)

    result = await wallets_mod.load_remote_wallets()

    # Ring 1 -> two entries (EVM + SVM) sharing the ring label, told apart by
    # chain_type, each with its own session expiry. Ring 2 -> one entry.
    assert len(result) == 3
    evm1, svm1, evm2 = result

    assert evm1["label"] == svm1["label"] == "solar-wind"
    assert evm1["address"] == "0x000000000000000000000000000000000000dEaD"
    assert evm1["chain_type"] == "ethereum"
    assert evm1["session_expires_at"] == 1784224019

    assert svm1["address"] == "So11111111111111111111111111111111111111112"
    assert svm1["chain_type"] == "solana"
    assert svm1["session_expires_at"] == 1784224999

    assert evm2["label"] == "lone-oak"
    assert evm2["chain_type"] == "ethereum"
    assert evm2["session_expires_at"] is None


@pytest.mark.asyncio
async def test_load_remote_wallets_omits_svm_when_solana_disabled(monkeypatch):
    _setup(monkeypatch, solana_enabled=False)

    result = await wallets_mod.load_remote_wallets()

    # SVM legs are dropped — only the two EVM legs remain.
    assert [w["chain_type"] for w in result] == ["ethereum", "ethereum"]


def test_public_wallet_view_shape():
    view = public_wallet_view(
        {
            "label": "solar-wind",
            "address": "So11111111111111111111111111111111111111112",
            "chain_type": "solana",
            "wallet_type": "session",
            "session_expires_at": 1784224999,
            "session_expires_in": 3421,
        }
    )
    assert view["label"] == "solar-wind"
    assert view["address"] == "So11111111111111111111111111111111111111112"
    assert view["chain_type"] == "solana"
    assert view["session_expires_at"] == 1784224999
    assert "svm_address" not in view
