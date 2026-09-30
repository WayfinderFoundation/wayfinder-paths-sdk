from typing import Any
from unittest.mock import AsyncMock

import pytest

from wayfinder_paths.mcp.tools import hyperliquid as tools


@pytest.fixture(autouse=True)
def no_metric_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "wayfinder_paths.mcp.utils._report_tool_metric", lambda *args: None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,asset_id,coin",
    [
        ("HYPE/USDC", 10107, "@107"),
        ("PURR/USDC", 10000, "PURR/USDC"),
        ("kBONK-USDC", 1, "kBONK"),
        ("xyz:GOLD", 110001, "xyz:GOLD"),
    ],
)
async def test_public_depth_preserves_asset_identity_and_filters_band(
    monkeypatch: pytest.MonkeyPatch, name: str, asset_id: int, coin: str
) -> None:
    monkeypatch.setattr(
        tools.HyperliquidAdapter,
        "get_all_mid_prices",
        AsyncMock(return_value=(True, {coin: "100"})),
    )
    monkeypatch.setattr(
        tools.HyperliquidAdapter, "get_asset_id", AsyncMock(return_value=asset_id)
    )
    raw = {
        "time": 12345678,
        "coin": coin,
        "levels": [
            [
                {"px": "99.9", "sz": "10"},
                {"px": "99", "sz": "1000000"},
                {"px": "NaN", "sz": "10"},
            ],
            [
                {"px": "100.1", "sz": "20"},
                {"px": "101", "sz": "1000000"},
                {"px": "Infinity", "sz": "10"},
            ],
        ],
    }
    post = AsyncMock(return_value=raw)
    monkeypatch.setattr(tools.HYPERLIQUID_INFO_CLIENT, "post", post)
    result = await tools.hyperliquid_search_mid_prices([name], include_depth=True)
    assert result["ok"]
    book = result["result"]["depth"][name]
    assert book["observed_at_ms"] == raw["time"]
    assert book["bid_notional_usd_50bps"] == pytest.approx(999)
    assert book["ask_notional_usd_50bps"] == pytest.approx(2002)
    post.assert_awaited_once_with({"type": "l2Book", "coin": coin})
    assert book["band_complete"]
    assert book["aggregation_sig_figs"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("four_digit_covers_band", [False, True])
async def test_depth_widens_aggregation_without_double_counting_or_moving_mid(
    monkeypatch: pytest.MonkeyPatch, four_digit_covers_band: bool
) -> None:
    monkeypatch.setattr(
        tools.HyperliquidAdapter,
        "get_all_mid_prices",
        AsyncMock(return_value=(True, {"BTC": 100})),
    )
    monkeypatch.setattr(
        tools.HyperliquidAdapter, "get_asset_id", AsyncMock(return_value=0)
    )

    def snapshot(bid: float, ask: float, step: float) -> dict[str, Any]:
        return {
            "time": 12345678,
            "levels": [
                [{"px": bid - step * n, "sz": 1} for n in range(20)],
                [{"px": ask + step * n, "sz": 1} for n in range(20)],
            ],
        }

    wide = snapshot(99.8, 100.3, 0.1)
    post = AsyncMock(
        side_effect=[
            snapshot(99.99, 100.01, 0.001),
            wide if four_digit_covers_band else snapshot(99.9, 100.1, 0.01),
            wide,
        ]
    )
    monkeypatch.setattr(tools.HYPERLIQUID_INFO_CLIENT, "post", post)
    result = await tools.hyperliquid_search_mid_prices(["BTC-USDC"], include_depth=True)
    book = result["result"]["depth"]["BTC-USDC"]
    assert book["mid_px"] == 100
    assert book["bid_notional_usd_50bps"] == pytest.approx(398.6)
    assert book["ask_notional_usd_50bps"] == pytest.approx(301.2)
    assert book["band_complete"]
    assert book["aggregation_sig_figs"] == (4 if four_digit_covers_band else 3)
    requests = [call.args[0] for call in post.await_args_list]
    assert [request.get("nSigFigs") for request in requests] == (
        [None, 4] if four_digit_covers_band else [None, 4, 3]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("names", [None, [], ["HYPE/UBTC"], ["BTC-USDC"] * 9])
async def test_depth_requires_bounded_exact_usd_assets(names: list[str] | None) -> None:
    result = await tools.hyperliquid_search_mid_prices(names, include_depth=True)
    assert not result["ok"]


@pytest.mark.asyncio
async def test_default_mid_price_read_does_not_fetch_books(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tools.HyperliquidAdapter,
        "get_all_mid_prices",
        AsyncMock(return_value=(True, {"BTC": "100"})),
    )
    monkeypatch.setattr(
        tools.HyperliquidAdapter, "get_asset_id", AsyncMock(return_value=0)
    )
    post = AsyncMock(side_effect=AssertionError("Default read must not request depth"))
    monkeypatch.setattr(tools.HYPERLIQUID_INFO_CLIENT, "post", post)
    result = await tools.hyperliquid_search_mid_prices(["BTC-USDC"])
    assert result["result"] == {"prices": {"BTC-USDC": "100"}}
    post.assert_not_awaited()
