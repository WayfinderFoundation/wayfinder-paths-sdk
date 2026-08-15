"""Live-broker state parsing against the REAL hyperliquid_get_state shape.

The engine's fetch_state parsed the pre-unified `perp.state.assetPositions`
shape long after the tool moved to flat `perp_positions` + `summary` — every
live tick raised "perp state fetch unsuccessful", reconcile_mismatch wakes
fired for 25 minutes, and the advisor reverted the operator's live switch
(observed live on majors-5m-lab). No test exercised the real parser, so the
drift shipped silently; this one pins the CURRENT tool shape."""

from __future__ import annotations

import pytest

import wayfinder_paths.jobs.execution.hyperliquid as hl_module
from wayfinder_paths.jobs.execution.hyperliquid import HyperliquidPerpBroker

UNIFIED_STATE = {
    "label": "funding-carry-basket",
    "address": "0x283b6931F4c7610F7235cE3b3cF50fDfd0b7e487",
    "account_abstraction": "unifiedAccount",
    "summary": {
        "unified_usdc_equity": 109.86377,
        "unified_usdc_unrealized_pnl": 1.2,
        "unified_usdc_margin_used": 20.0,
        "unified_usdc_margin_available": 89.86,
    },
    "perp_positions": [
        {"coin": "SOL", "szi": "2.5", "entryPx": "150.0", "asset_name": "SOL-USDC"},
        {"coin": "HYPE", "szi": "-10", "entryPx": "40.5", "asset_name": "HYPE-USDC"},
        {"coin": "XRP", "szi": "0", "entryPx": "2.0"},  # flat — skipped
    ],
    "spot_positions": [],
    "outcome_positions": [],
    "open_orders": [],
}


@pytest.mark.asyncio
async def test_fetch_state_parses_unified_shape(monkeypatch) -> None:
    async def fake_state(wallet_label: str):
        assert wallet_label == "funding-carry-basket"
        return dict(UNIFIED_STATE)

    monkeypatch.setattr(hl_module, "_hl_state_result", fake_state)
    broker = HyperliquidPerpBroker(wallet_label="funding-carry-basket")
    state = await broker.fetch_state()

    assert set(state.positions) == {"SOL", "HYPE"}
    assert state.positions["SOL"].side == "long"
    assert state.positions["SOL"].size == 2.5
    assert state.positions["SOL"].avg_price == 150.0
    assert state.positions["HYPE"].side == "short"
    assert state.positions["HYPE"].size == 10.0
    assert state.balances["accountValue"] == 109.86377


@pytest.mark.asyncio
async def test_fetch_state_classic_account_balance(monkeypatch) -> None:
    async def fake_state(wallet_label: str):
        return {
            "account_abstraction": "default",
            "summary": {"perp_account_value": 55.5, "spot_usdc_total": 10.0},
            "perp_positions": [],
            "open_orders": [],
        }

    monkeypatch.setattr(hl_module, "_hl_state_result", fake_state)
    broker = HyperliquidPerpBroker(wallet_label="main")
    state = await broker.fetch_state()
    assert state.positions == {}
    assert state.balances["accountValue"] == 55.5


@pytest.mark.asyncio
async def test_fetch_state_rejects_unknown_shape(monkeypatch) -> None:
    async def fake_state(wallet_label: str):
        # The pre-unified shape (or any future drift) must fail LOUDLY with
        # the shape named — not a misleading "fetch unsuccessful".
        return {"perp": {"success": True, "state": {"assetPositions": []}}}

    monkeypatch.setattr(hl_module, "_hl_state_result", fake_state)
    broker = HyperliquidPerpBroker(wallet_label="main")
    with pytest.raises(RuntimeError, match="unexpected shape"):
        await broker.fetch_state()
