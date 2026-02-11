from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from wayfinder_paths.mcp.tools.wallets import wallets
from wayfinder_paths.mcp.utils import resolve_wallet_address


def test_resolve_wallet_address_prefers_explicit_address():
    addr, lbl = resolve_wallet_address(
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
