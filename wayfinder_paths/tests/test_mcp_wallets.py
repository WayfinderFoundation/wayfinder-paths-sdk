from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.mcp.resources.wallets import get_wallet
from wayfinder_paths.mcp.tools.wallets import wallets
from wayfinder_paths.mcp.utils import resolve_wallet_address


@pytest.mark.asyncio
async def test_resolve_wallet_address_prefers_explicit_address():
    addr, lbl = await resolve_wallet_address(
        wallet_label="main", wallet_address="0x000000000000000000000000000000000000dEaD"
    )
    assert addr == "0x000000000000000000000000000000000000dEaD"
    assert lbl is None


@pytest.mark.asyncio
async def test_wallets_discover_portfolio_requires_confirmation_when_many_protocols():
    store = SimpleNamespace(
        get_protocols_for_wallet=lambda _addr: ["hyperliquid", "pendle", "moonwell"]
    )  # noqa: E501

    with patch(
        "wayfinder_paths.mcp.tools.wallets.WalletProfileStore.default",
        return_value=store,
    ):
        out = await wallets(
            "discover_portfolio",
            wallet_address="0x000000000000000000000000000000000000dEaD",
            parallel=False,
        )

    assert out["ok"] is True
    res = out["result"]
    assert res["requires_confirmation"] is True
    assert set(res["protocols_to_query"]) == {"hyperliquid", "pendle", "moonwell"}


@pytest.mark.asyncio
async def test_wallets_policy_status_action():
    with patch(
        "wayfinder_paths.mcp.tools.wallets.get_remote_wallet_policy_status",
        new=AsyncMock(
            return_value={
                "time_bound": True,
                "effective_ttl_seconds": 3600,
                "ttl_source": "built_in_default",
                "expires_at": "2026-01-01T00:00:00+00:00",
                "remaining_seconds": 120,
                "source": "remote",
            }
        ),
    ):
        out = await wallets(
            "policy_status",
            wallet_address="0x000000000000000000000000000000000000dEaD",
        )

    assert out["ok"] is True
    assert out["result"]["policy_status"]["time_bound"] is True
    assert out["result"]["policy_status"]["remaining_seconds"] == 120


@pytest.mark.asyncio
async def test_wallets_refresh_policy_action():
    with patch(
        "wayfinder_paths.mcp.tools.wallets.refresh_remote_wallet_policy",
        new=AsyncMock(
            return_value={
                "wallet_address": "0x000000000000000000000000000000000000dEaD",
                "policies": [{"name": "Allow swap [wayfinder-timebound]"}],
                "policy_status": {
                    "time_bound": True,
                    "effective_ttl_seconds": 3600,
                    "ttl_source": "built_in_default",
                    "expires_at": "2026-01-01T00:00:00+00:00",
                    "remaining_seconds": 3599,
                    "source": "remote",
                },
                "updated": True,
            }
        ),
    ):
        out = await wallets(
            "refresh_policy",
            wallet_address="0x000000000000000000000000000000000000dEaD",
        )

    assert out["ok"] is True
    assert out["result"]["updated"] is True
    assert out["result"]["policy_status"]["time_bound"] is True


@pytest.mark.asyncio
async def test_get_wallet_includes_remote_policy_status():
    store = SimpleNamespace(get_profile=lambda _addr: {"tracked": []})
    wallet = {
        "label": "remote-main",
        "address": "0x000000000000000000000000000000000000dEaD",
        "type": "remote",
    }

    with (
        patch(
            "wayfinder_paths.mcp.resources.wallets.WalletProfileStore.default",
            return_value=store,
        ),
        patch(
            "wayfinder_paths.mcp.resources.wallets.find_wallet_by_label",
            new=AsyncMock(return_value=wallet),
        ),
        patch(
            "wayfinder_paths.mcp.resources.wallets.get_remote_wallet_policy_status",
            new=AsyncMock(
                return_value={
                    "time_bound": True,
                    "effective_ttl_seconds": 3600,
                    "ttl_source": "built_in_default",
                    "expires_at": "2026-01-01T00:00:00+00:00",
                    "remaining_seconds": 100,
                    "source": "remote",
                }
            ),
        ),
    ):
        payload = await get_wallet("remote-main")

    assert '"policy_status"' in payload
    assert '"remaining_seconds": 100' in payload
