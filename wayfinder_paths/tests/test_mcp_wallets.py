from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.mcp.tools.wallets import (
    core_wallets,
    onchain_get_wallet_activity,
)
from wayfinder_paths.mcp.utils import resolve_wallet_address


@pytest.mark.asyncio
async def test_resolve_wallet_address_prefers_explicit_address():
    addr, lbl = await resolve_wallet_address(
        wallet_label="main", wallet_address="0x000000000000000000000000000000000000dEaD"
    )
    assert addr == "0x000000000000000000000000000000000000dEaD"
    assert lbl is None


@pytest.mark.asyncio
async def test_resolve_wallet_address_unknown_label_preserves_label():
    # An unknown label must come back as (None, label) so callers can emit a
    # "Unknown wallet_label: X" 404 instead of a misleading generic error.
    with patch("wayfinder_paths.mcp.utils.find_wallet_by_label", return_value=None):
        addr, lbl = await resolve_wallet_address(wallet_label="not-a-real-label")
    assert addr is None
    assert lbl == "not-a-real-label"


@pytest.mark.asyncio
async def test_resolve_wallet_address_missing_label_returns_double_none():
    addr, lbl = await resolve_wallet_address()
    assert addr is None
    assert lbl is None


@pytest.mark.asyncio
async def test_wallet_activity_accepts_raw_address_with_pagination():
    mock = AsyncMock(return_value={"activity": [{"id": 1}], "next_offset": "cursor"})
    with patch(
        "wayfinder_paths.mcp.tools.wallets.BALANCE_CLIENT.get_wallet_activity", mock
    ):
        out = await onchain_get_wallet_activity(
            wallet_address="0x000000000000000000000000000000000000dEaD",
            limit=50,
            offset="prev-cursor",
        )

    mock.assert_awaited_once_with(
        wallet_address="0x000000000000000000000000000000000000dEaD",
        limit=50,
        offset="prev-cursor",
    )
    assert out["ok"] is True
    assert out["result"]["label"] is None
    assert out["result"]["next_offset"] == "cursor"


@pytest.mark.asyncio
async def test_wallet_activity_resolves_label():
    mock = AsyncMock(return_value={"activity": [], "next_offset": None})
    with (
        patch(
            "wayfinder_paths.mcp.utils.find_wallet_by_label",
            return_value={"address": "0x000000000000000000000000000000000000bEEF"},
        ),
        patch(
            "wayfinder_paths.mcp.tools.wallets.BALANCE_CLIENT.get_wallet_activity", mock
        ),
    ):
        out = await onchain_get_wallet_activity(label="main")

    mock.assert_awaited_once_with(
        wallet_address="0x000000000000000000000000000000000000bEEF",
        limit=20,
        offset=None,
    )
    assert out["result"]["label"] == "main"


@pytest.mark.asyncio
async def test_wallet_activity_requires_label_or_address():
    out = await onchain_get_wallet_activity()
    assert out["ok"] is False
    assert out["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_wallets_discover_portfolio_requires_confirmation_when_many_protocols():
    store = SimpleNamespace(
        get_protocols_for_wallet=lambda _addr: ["hyperliquid", "pendle", "moonwell"]
    )  # noqa: E501

    with patch(
        "wayfinder_paths.mcp.tools.wallets.WalletProfileStore.default",
        return_value=store,
    ):
        out = await core_wallets(
            "discover_portfolio",
            wallet_address="0x000000000000000000000000000000000000dEaD",
            parallel=False,
        )

    assert out["ok"] is True
    res = out["result"]
    assert res["requires_confirmation"] is True
    assert set(res["protocols_to_query"]) == {"hyperliquid", "pendle", "moonwell"}
