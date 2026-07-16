from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from wayfinder_paths.core.utils import wallets as wallets_mod
from wayfinder_paths.mcp.utils import public_wallet_view


def _ring_side(*, chain_type: str, address: str) -> dict[str, object]:
    return {
        "wallet_address": address,
        "chain_type": chain_type,
        "label": "side",
        "is_active": True,
        "wallet_type": "session",
        "policy_id": "pol-1",
        "created": "2026-01-01T00:00:00Z",
    }


@pytest.mark.asyncio
async def test_load_remote_wallets_reads_rings(monkeypatch):
    monkeypatch.setattr(wallets_mod, "get_api_key", lambda: "wk_test")
    monkeypatch.setattr(wallets_mod, "is_opencode_instance", lambda: True)
    monkeypatch.setattr(wallets_mod, "get_opencode_instance_id", lambda: "app-123")
    monkeypatch.setattr(
        wallets_mod.WALLET_CLIENT,
        "list_wallet_rings",
        AsyncMock(
            return_value=[
                {
                    "label": "Session 1",
                    "evm": _ring_side(
                        chain_type="ethereum",
                        address="0x000000000000000000000000000000000000dEaD",
                    ),
                    "svm": _ring_side(
                        chain_type="solana",
                        address="So11111111111111111111111111111111111111112",
                    ),
                    "session_expires_at": 1784224019,
                    "session_expires_in": 3421,
                },
                {
                    "label": "Session 2",
                    "evm": _ring_side(
                        chain_type="ethereum",
                        address="0x000000000000000000000000000000000000bEEF",
                    ),
                    "svm": None,
                },
            ]
        ),
    )

    result = await wallets_mod.load_remote_wallets()

    # Ring label is the source of truth; EVM address is the primary identity.
    assert result[0]["label"] == "Session 1"
    assert result[0]["address"] == "0x000000000000000000000000000000000000dEaD"
    assert result[0]["svm_address"] == "So11111111111111111111111111111111111111112"
    assert result[0]["session_expires_at"] == 1784224019
    assert result[0]["session_expires_in"] == 3421

    assert result[1]["label"] == "Session 2"
    assert result[1]["svm_address"] is None
    assert result[1]["session_expires_at"] is None


def test_public_wallet_view_surfaces_svm_when_present():
    view = public_wallet_view(
        {
            "label": "Session 1",
            "address": "0x000000000000000000000000000000000000dEaD",
            "svm_address": "So11111111111111111111111111111111111111112",
        }
    )
    assert view["label"] == "Session 1"
    assert view["svm_address"] == "So11111111111111111111111111111111111111112"


def test_public_wallet_view_omits_svm_when_absent():
    view = public_wallet_view(
        {
            "label": "Session 2",
            "address": "0x000000000000000000000000000000000000bEEF",
            "svm_address": None,
        }
    )
    assert "svm_address" not in view
