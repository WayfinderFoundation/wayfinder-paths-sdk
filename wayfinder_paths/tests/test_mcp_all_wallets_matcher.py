from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.mcp.tools.hyperliquid import hyperliquid_get_state
from wayfinder_paths.mcp.utils import expand_all_wallets

WALLETS = [
    {"label": "wallet-one", "address": "0x000000000000000000000000000000000000dEaD"},
    {"label": "wallet-two", "address": "0x000000000000000000000000000000000000bEEF"},
]


@pytest.mark.asyncio
async def test_expand_all_wallets_matches_the_sentinel():
    with (
        patch(
            "wayfinder_paths.mcp.utils.find_wallet_by_label",
            AsyncMock(return_value=None),
        ),
        patch(
            "wayfinder_paths.mcp.utils.load_wallets", AsyncMock(return_value=WALLETS)
        ),
    ):
        assert await expand_all_wallets("all") == ["wallet-one", "wallet-two"]
        assert await expand_all_wallets(" ALL ") == ["wallet-one", "wallet-two"]


@pytest.mark.asyncio
async def test_expand_all_wallets_ignores_normal_labels_and_empty():
    assert await expand_all_wallets("wallet-one") is None
    assert await expand_all_wallets("") is None
    assert await expand_all_wallets(None) is None


@pytest.mark.asyncio
async def test_a_real_wallet_named_all_wins_over_the_sentinel():
    with patch(
        "wayfinder_paths.mcp.utils.find_wallet_by_label",
        AsyncMock(return_value={"label": "all", "address": "0x1"}),
    ):
        assert await expand_all_wallets("all") is None


@pytest.mark.asyncio
async def test_hyperliquid_get_state_all_returns_per_wallet_envelopes():
    async def fake_expand(label):
        return ["wallet-one", "wallet-two"] if label == "all" else None

    async def fake_resolve(*, wallet_label=None, wallet_address=None):
        # Per-wallet recursion: resolve each label to a distinct address, but
        # fail wallet-two to prove one broken wallet doesn't sink the rest.
        if wallet_label == "wallet-one":
            return "0x000000000000000000000000000000000000dEaD", wallet_label
        return None, wallet_label

    adapter = AsyncMock()
    adapter.get_user_state.return_value = (
        True,
        {
            "assetPositions": [],
            "marginSummary": {"accountValue": "100.0", "totalMarginUsed": "0"},
            "crossMaintenanceMarginUsed": "0",
            "withdrawable": "100.0",
        },
    )
    adapter.get_spot_user_state.return_value = (True, {"balances": []})
    adapter.get_user_abstraction.return_value = (True, "default")
    adapter.get_frontend_open_orders.return_value = (True, [])
    adapter.get_spot_assets.return_value = (True, {})
    adapter.canonical_asset_name = lambda coin, mapping: coin

    with (
        patch(
            "wayfinder_paths.mcp.tools.hyperliquid.expand_all_wallets",
            side_effect=fake_expand,
        ),
        patch(
            "wayfinder_paths.mcp.tools.hyperliquid.resolve_wallet_address",
            side_effect=fake_resolve,
        ),
        patch(
            "wayfinder_paths.mcp.tools.hyperliquid.HyperliquidAdapter",
            return_value=adapter,
        ),
    ):
        out = await hyperliquid_get_state("all")

    assert out["ok"] is True
    per_wallet = out["result"]["wallets"]
    assert set(per_wallet) == {"wallet-one", "wallet-two"}
    assert per_wallet["wallet-one"]["ok"] is True
    assert per_wallet["wallet-two"]["ok"] is False  # not_found envelope, not a raise


@pytest.mark.asyncio
async def test_core_get_wallets_all_label_lists_every_wallet():
    from types import SimpleNamespace

    from wayfinder_paths.mcp.tools.wallets import core_get_wallets

    store = SimpleNamespace(
        get_protocols_for_wallet=lambda _a: [],
        get_profile=lambda _a, transactions_limit=5: {},
    )
    with (
        patch(
            "wayfinder_paths.mcp.tools.wallets.expand_all_wallets",
            AsyncMock(return_value=["wallet-one", "wallet-two"]),
        ),
        patch(
            "wayfinder_paths.mcp.tools.wallets.load_wallets",
            AsyncMock(return_value=WALLETS),
        ),
        patch(
            "wayfinder_paths.mcp.tools.wallets.WalletProfileStore.default",
            return_value=store,
        ),
        patch(
            "wayfinder_paths.mcp.tools.wallets._fetch_balances",
            AsyncMock(return_value={"tokens": []}),
        ),
    ):
        out = await core_get_wallets(label="all")

    assert out["ok"] is True
    labels = [w["label"] for w in out["result"]["wallets"]]
    assert labels == ["wallet-one", "wallet-two"]
