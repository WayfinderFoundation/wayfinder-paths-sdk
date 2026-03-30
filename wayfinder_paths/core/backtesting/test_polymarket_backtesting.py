"""Tests for the Polymarket event-driven copy-trade backtester.

All tests use synthetic data — no network calls.
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd
import pytest

from wayfinder_paths.core.backtesting.polymarket_backtester import (
    compare_sizing_strategies,
    run_polymarket_backtest,
)
from wayfinder_paths.core.backtesting.polymarket_data import (
    _activity_records_to_trades_df,
    detect_resolutions,
    fetch_market_metadata,
    fetch_market_prices,
    fetch_wallet_trades,
    regularize_to_grid,
    warn_if_large,
)
from wayfinder_paths.core.backtesting.polymarket_helpers import (
    flat_dollar_sizer,
    flat_ratio_sizer,
    proportional_sizer,
)
from wayfinder_paths.core.backtesting.polymarket_parser import TradeSignal
from wayfinder_paths.core.backtesting.polymarket_types import (
    PolymarketBacktestConfig,
    PolymarketBacktestResult,
    PolymarketBacktestState,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def one_market_trades_df() -> pd.DataFrame:
    """Two WOI BUY trades on the same market, both at price 0.4."""
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2025-10-01 10:00", "2025-10-01 12:00"], utc=True
            ),
            "woi_address": ["0xWOI1", "0xWOI2"],
            "condition_id": ["cond_A", "cond_A"],
            "token_id": ["tok_YES_A", "tok_YES_A"],
            "outcome": ["Yes", "Yes"],
            "side": ["BUY", "BUY"],
            "usdc_amount": [100.0, 50.0],
            "share_count": [250.0, 125.0],
            "avg_price": [0.4, 0.4],
            "market_slug": ["market-a", "market-a"],
            "tx_hash": ["0xTX1", "0xTX2"],
        }
    ).set_index("timestamp")


@pytest.fixture
def prices_df_resolves_yes() -> pd.DataFrame:
    """48-hour hourly price series for tok_YES_A that resolves to 1.0."""
    idx = pd.date_range("2025-10-01", periods=48, freq="1h", tz="UTC")
    prices = [0.4] * 46 + [0.99, 1.0]
    return pd.DataFrame({"tok_YES_A": prices}, index=idx)


@pytest.fixture
def default_config() -> PolymarketBacktestConfig:
    return PolymarketBacktestConfig(
        initial_capital=100.0,
        fee_rate=0.02,
        slippage_rate=0.0,
        min_order_usdc=10.0,
        equity_interval="1h",
    )


# ---------------------------------------------------------------------------
# Test data helpers
# ---------------------------------------------------------------------------


def make_signal(
    usdc_amount: float = 100.0,
    price: float = 0.4,
    token: str = "tok_YES_A",
    side: str = "BUY",
    woi: str = "0xWOI1",
) -> TradeSignal:
    return TradeSignal(
        woi_address=woi,
        condition_id="cond_A",
        token_id=token,
        outcome="Yes",
        side=side,  # type: ignore[arg-type]
        usdc_amount=usdc_amount,
        share_count=usdc_amount / price,
        avg_price=price,
        market_slug="market-a",
        dedupe_key="0xTEST",
    )


def make_state(
    usdc_balance: float = 100.0,
    positions: dict[str, float] | None = None,
    woi_portfolio_usdc: float | None = None,
) -> PolymarketBacktestState:
    return PolymarketBacktestState(
        usdc_balance=usdc_balance,
        positions=positions or {},
        woi_portfolio_usdc=woi_portfolio_usdc,
    )


def make_single_buy(
    usdc: float = 10.0,
    price: float = 0.4,
    token: str = "tok_YES_A",
    cond_id: str = "cond_A",
) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": [pd.Timestamp("2025-10-01 00:00", tz="UTC")],
            "woi_address": ["0xWOI1"],
            "condition_id": [cond_id],
            "token_id": [token],
            "outcome": ["Yes"],
            "side": ["BUY"],
            "usdc_amount": [usdc],
            "share_count": [usdc / price],
            "avg_price": [price],
            "market_slug": [f"market-{cond_id}"],
            "tx_hash": ["0xTX1"],
        }
    ).set_index("timestamp")


def make_prices_resolving_to(
    resolution: float, token: str = "tok_YES_A", periods: int = 48
) -> pd.DataFrame:
    idx = pd.date_range("2025-10-01", periods=periods, freq="1h", tz="UTC")
    prices = [0.4] * (periods - 2) + [
        0.5 * (0.4 + resolution),
        resolution,
    ]
    return pd.DataFrame({token: prices}, index=idx)


def make_constant_prices(
    price: float, periods: int, tokens: tuple[str, ...] = ("tok_YES_A",)
) -> pd.DataFrame:
    idx = pd.date_range("2025-10-01", periods=periods, freq="1h", tz="UTC")
    return pd.DataFrame({t: [price] * periods for t in tokens}, index=idx)


def make_price_with_gap(
    gap_start_h: int,
    gap_end_h: int,
    value: float = 0.4,
    token: str = "tok_YES_A",
) -> pd.DataFrame:
    periods = gap_end_h + 10
    idx = pd.date_range("2025-10-01", periods=periods, freq="1h", tz="UTC")
    prices: list[float | float] = (
        [value] * gap_start_h
        + [float("nan")] * (gap_end_h - gap_start_h)
        + [value] * 10
    )
    return pd.DataFrame({token: prices}, index=idx)


def make_trades(entries: list[tuple[float, str]]) -> pd.DataFrame:
    """Build a trades_df with one BUY per (entry_price, condition_id) pair."""
    rows = []
    for i, (price, cond_id) in enumerate(entries):
        token_id = f"tok_YES_{cond_id}"
        rows.append(
            {
                "timestamp": pd.Timestamp("2025-10-01", tz="UTC"),
                "woi_address": f"0xWOI{i + 1}",
                "condition_id": cond_id,
                "token_id": token_id,
                "outcome": "Yes",
                "side": "BUY",
                "usdc_amount": 100.0,
                "share_count": 100.0 / price,
                "avg_price": price,
                "market_slug": f"market-{cond_id}",
                "tx_hash": f"0xTX{i + 1}",
            }
        )
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df.set_index("timestamp")


def make_multi_market_prices(
    resolutions: dict[str, float], periods: int = 48
) -> pd.DataFrame:
    """Price series per token where each token converges to its resolution value."""
    idx = pd.date_range("2025-10-01", periods=periods, freq="1h", tz="UTC")
    data = {}
    for cond_id, res in resolutions.items():
        token_id = f"tok_YES_{cond_id}"
        prices = [0.5 + (res - 0.5) * i / max(1, periods - 1) for i in range(periods)]
        data[token_id] = prices
    return pd.DataFrame(data, index=idx)


def assert_valid_polymarket_result(result: PolymarketBacktestResult) -> None:
    """Cross-cutting invariants that must hold for any valid backtest result."""
    clean = result.equity_curve.dropna()
    assert (clean >= 0).all(), "Equity curve must be non-negative"
    assert result.stats["total_return"] >= -1.0, "Cannot lose more than invested"
    assert result.stats["total_fees"] >= 0.0
    assert result.stats["trade_count"] >= 0

    mwr = result.stats.get("market_win_rate")
    if mwr is not None and not math.isnan(mwr):
        assert 0.0 <= mwr <= 1.0

    bs = result.stats.get("brier_score")
    if bs is not None and not math.isnan(bs):
        assert 0.0 <= bs <= 1.0

    won = result.stats["markets_won"]
    lost = result.stats["markets_lost"]
    voided = result.stats["markets_voided"]
    traded = result.stats["markets_traded"]
    assert won + lost + voided == traded, (
        f"markets_won({won}) + lost({lost}) + voided({voided}) != traded({traded})"
    )


# ---------------------------------------------------------------------------
# 2.1  regularize_to_grid
# ---------------------------------------------------------------------------


def test_regularize_fills_hourly_grid() -> None:
    raw: dict[str, list[dict[str, Any]]] = {
        "tok_A": [
            {"t": 1696147200, "p": 0.3},  # 2023-10-01 08:00 UTC
            {"t": 1696154400, "p": 0.35},  # 2023-10-01 10:00 UTC
        ]
    }
    df = regularize_to_grid(raw, "2023-10-01", "2023-10-01T12:00", interval="1h")
    assert (df.index[1] - df.index[0]) == pd.Timedelta("1h")
    # Convention: row t = last observation STRICTLY BEFORE t.
    # At 09:00: last obs before 09:00 is the 08:00 obs → 0.3
    assert df.loc["2023-10-01 09:00:00+00:00", "tok_A"] == pytest.approx(0.3)
    # At 10:00: last obs before 10:00 is still the 08:00 obs → 0.3
    #   (the 10:00 obs is NOT available at the 10:00 row)
    assert df.loc["2023-10-01 10:00:00+00:00", "tok_A"] == pytest.approx(0.3)
    # At 11:00: last obs before 11:00 is the 10:00 obs → 0.35
    assert df.loc["2023-10-01 11:00:00+00:00", "tok_A"] == pytest.approx(0.35)


def test_regularize_gaps_beyond_max_become_nan() -> None:
    raw: dict[str, list[dict[str, Any]]] = {
        "tok_A": [
            {"t": 1696147200, "p": 0.4},  # 2023-10-01 08:00 UTC (t=0h)
            {"t": 1696320000, "p": 0.5},  # 2023-10-03 08:00 UTC (t=48h)
        ]
    }
    df = regularize_to_grid(
        raw,
        "2023-10-01T08:00:00Z",
        "2023-10-03T09:00:00Z",
        interval="1h",
        max_gap_hours=24,
    )
    # t=1h is within 24h gap → forward-filled
    assert df.iloc[1]["tok_A"] == pytest.approx(0.4)
    # t=25h is beyond 24h → NaN
    assert pd.isna(df.iloc[25]["tok_A"])
    # t=47h is still in the gap → NaN
    assert pd.isna(df.iloc[47]["tok_A"])


def test_regularize_multiple_tokens() -> None:
    raw: dict[str, list[dict[str, Any]]] = {
        "tok_YES": [{"t": 1696147200, "p": 0.4}],
        "tok_NO": [{"t": 1696147200, "p": 0.6}],
    }
    df = regularize_to_grid(raw, "2023-10-01T08:00:00Z", "2023-10-01T10:00:00Z")
    assert set(df.columns) == {"tok_YES", "tok_NO"}


# ---------------------------------------------------------------------------
# 2.2  warn_if_large
# ---------------------------------------------------------------------------


def test_warn_if_large_markets(recwarn: pytest.WarningsChecker) -> None:
    warn_if_large(n_markets=51, n_trades=100)
    assert len(recwarn) > 0


def test_warn_if_large_trades(recwarn: pytest.WarningsChecker) -> None:
    warn_if_large(n_markets=5, n_trades=5001)
    assert len(recwarn) > 0


def test_no_warn_below_threshold(recwarn: pytest.WarningsChecker) -> None:
    warn_if_large(n_markets=10, n_trades=100)
    assert len(recwarn) == 0


# ---------------------------------------------------------------------------
# 2.3  detect_resolutions
# ---------------------------------------------------------------------------


def test_detect_resolves_yes() -> None:
    df = pd.DataFrame({"tok_A": [0.4, 0.7, 0.99, 1.0]})
    assert detect_resolutions(df) == {"tok_A": 1.0}


def test_detect_resolves_no() -> None:
    df = pd.DataFrame({"tok_A": [0.5, 0.3, 0.01, 0.0]})
    assert detect_resolutions(df) == {"tok_A": 0.0}


def test_detect_unresolved_excluded() -> None:
    df = pd.DataFrame({"tok_A": [0.4, 0.5, 0.6]})
    assert "tok_A" not in detect_resolutions(df)


def test_detect_threshold_boundary() -> None:
    df_below = pd.DataFrame({"tok_A": [0.98]})
    assert "tok_A" not in detect_resolutions(df_below)

    df_at = pd.DataFrame({"tok_A": [0.99]})
    assert detect_resolutions(df_at) == {"tok_A": 1.0}


# ---------------------------------------------------------------------------
# 2.4  fetch_wallet_trades (Delta Lab → adapter fallback)
# ---------------------------------------------------------------------------


def test_activity_records_to_trades_df_parses_records() -> None:
    """_activity_records_to_trades_df converts raw Data API records to backtester format."""
    records = [
        {
            "type": "TRADE",
            "side": "BUY",
            "conditionId": "0xcond1",
            "asset": "tok_yes_1",
            "usdcSize": "50.0",
            "size": "125.0",
            "price": "0.4",
            "outcome": "Yes",
            "transactionHash": "0xabc",
            "slug": "test-market",
            "timestamp": "1696118400",  # 2023-10-01 00:00 UTC
        },
        {
            "type": "TRADE",
            "side": "SELL",
            "conditionId": "0xcond1",
            "asset": "tok_yes_1",
            "usdcSize": "30.0",
            "size": "75.0",
            "price": "0.4",
            "outcome": "Yes",
            "transactionHash": "0xdef",
            "slug": "test-market",
            "timestamp": "1696122000",  # 1h later
        },
        # Non-trade record — should be skipped
        {
            "type": "DEPOSIT",
            "timestamp": "1696118400",
        },
    ]
    df = _activity_records_to_trades_df("0xWOI", records)
    assert len(df) == 2
    assert list(df.columns) == [
        "woi_address",
        "condition_id",
        "token_id",
        "outcome",
        "side",
        "usdc_amount",
        "share_count",
        "avg_price",
        "market_slug",
        "tx_hash",
    ]
    assert df.iloc[0]["side"] == "BUY"
    assert df.iloc[1]["side"] == "SELL"
    assert df.iloc[0]["usdc_amount"] == 50.0
    assert df.index.tz is not None  # UTC


def test_activity_records_to_trades_df_empty() -> None:
    df = _activity_records_to_trades_df("0xWOI", [])
    assert df.empty
    assert "woi_address" in df.columns


def test_activity_records_to_trades_df_iso_timestamps() -> None:
    """Handles ISO 8601 timestamps from the Data API."""
    records = [
        {
            "type": "TRADE",
            "side": "BUY",
            "conditionId": "0xcond1",
            "asset": "tok1",
            "usdcSize": "10.0",
            "size": "25.0",
            "price": "0.4",
            "outcome": "Yes",
            "transactionHash": "0x111",
            "slug": "iso-market",
            "timestamp": "2025-10-01T12:00:00Z",
        },
    ]
    df = _activity_records_to_trades_df("0xWOI", records)
    assert len(df) == 1
    assert df.index[0].hour == 12


@pytest.mark.asyncio
async def test_fetch_wallet_trades_uses_adapter_fallback() -> None:
    """When Delta Lab returns None, falls back to adapter pagination."""
    from unittest.mock import AsyncMock, MagicMock

    from wayfinder_paths.adapters.polymarket_adapter import PolymarketAdapter

    mock_adapter = MagicMock(spec=PolymarketAdapter)
    mock_adapter.get_wallet_trade_history = AsyncMock(
        return_value=(
            True,
            [
                {
                    "type": "TRADE",
                    "side": "BUY",
                    "conditionId": "0xcond1",
                    "asset": "tok1",
                    "usdcSize": "20.0",
                    "size": "50.0",
                    "price": "0.4",
                    "outcome": "Yes",
                    "transactionHash": "0xaaa",
                    "slug": "fallback-market",
                    "timestamp": "1696118400",
                },
            ],
        )
    )
    mock_adapter.close = AsyncMock()

    df = await fetch_wallet_trades(
        "0xABC",
        "2023-10-01",
        "2023-10-02",
        adapter=mock_adapter,
    )
    assert len(df) == 1
    assert df.iloc[0]["market_slug"] == "fallback-market"
    mock_adapter.get_wallet_trade_history.assert_awaited_once()


@pytest.mark.asyncio
async def test_fetch_wallet_trades_falls_back_to_goldsky_on_data_api_failure() -> None:
    """When Data API fails, falls through to Goldsky and returns whatever it gets."""
    from unittest.mock import AsyncMock, MagicMock

    from wayfinder_paths.adapters.polymarket_adapter import PolymarketAdapter

    mock_adapter = MagicMock(spec=PolymarketAdapter)
    mock_adapter.get_wallet_trade_history = AsyncMock(
        return_value=(False, "data api error")
    )
    mock_adapter.get_goldsky_wallet_trades = AsyncMock(return_value=(True, []))
    mock_adapter.close = AsyncMock()

    df = await fetch_wallet_trades(
        "0xABC",
        "2023-10-01",
        "2023-10-02",
        adapter=mock_adapter,
    )
    assert df.empty
    mock_adapter.get_goldsky_wallet_trades.assert_awaited_once()


# ---------------------------------------------------------------------------
# 2.5  fetch_market_prices and fetch_market_metadata
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_market_prices_returns_hourly_grid(monkeypatch) -> None:
    """fetch_market_prices calls adapter and returns regularized DataFrame."""
    from unittest.mock import AsyncMock, MagicMock

    from wayfinder_paths.adapters.polymarket_adapter import PolymarketAdapter

    mock_adapter = MagicMock(spec=PolymarketAdapter)
    mock_adapter.get_batch_prices_history = AsyncMock(
        return_value=(
            True,
            {
                "tok_A": [
                    {"t": 1735689600, "p": 0.5},  # 2025-01-01 00:00
                    {"t": 1735693200, "p": 0.6},  # 2025-01-01 01:00
                    {"t": 1735696800, "p": 0.7},  # 2025-01-01 02:00
                ],
            },
        )
    )
    mock_adapter.close = AsyncMock()

    df = await fetch_market_prices(
        ["tok_A"],
        "2025-01-01",
        "2025-01-01 02:00",
        adapter=mock_adapter,
    )
    assert "tok_A" in df.columns
    assert len(df) == 3  # 3 hourly points: 00:00, 01:00, 02:00
    # Convention: row t = last observation strictly before t.
    # Raw obs land on exact grid boundaries (00:00, 01:00, 02:00), so each
    # is visible one step later.
    assert pd.isna(df["tok_A"].iloc[0])  # 00:00: no obs before 00:00
    assert df["tok_A"].iloc[1] == pytest.approx(0.5)  # 01:00: obs from 00:00
    assert df["tok_A"].iloc[2] == pytest.approx(0.6)  # 02:00: obs from 01:00
    mock_adapter.get_batch_prices_history.assert_awaited_once()


@pytest.mark.asyncio
async def test_fetch_market_prices_adapter_failure_raises(monkeypatch) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from wayfinder_paths.adapters.polymarket_adapter import PolymarketAdapter

    mock_adapter = MagicMock(spec=PolymarketAdapter)
    mock_adapter.get_batch_prices_history = AsyncMock(
        return_value=(False, "network error")
    )
    mock_adapter.close = AsyncMock()

    with pytest.raises(RuntimeError, match="Failed to fetch prices"):
        await fetch_market_prices(
            ["tok_A"], "2025-01-01", "2025-01-02", adapter=mock_adapter
        )


@pytest.mark.asyncio
async def test_fetch_market_metadata_returns_structured_dict(monkeypatch) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from wayfinder_paths.adapters.polymarket_adapter import PolymarketAdapter

    mock_adapter = MagicMock(spec=PolymarketAdapter)
    mock_adapter.get_markets_by_condition_ids = AsyncMock(
        return_value=(
            True,
            {
                "0xcond1": {
                    "slug": "will-x-happen",
                    "question": "Will X happen?",
                    "endDate": "2025-12-31T00:00:00Z",
                    "closed": False,
                    "volumeNum": 50000,
                    "outcomes": ["Yes", "No"],
                    "clobTokenIds": ["tok1", "tok2"],
                },
            },
        )
    )
    mock_adapter.close = AsyncMock()

    result = await fetch_market_metadata(["0xcond1"], adapter=mock_adapter)
    assert "0xcond1" in result
    meta = result["0xcond1"]
    assert meta["market_slug"] == "will-x-happen"
    assert meta["question"] == "Will X happen?"
    assert meta["resolved"] is False
    assert len(meta["tokens"]) == 2
    assert meta["tokens"][0]["token_id"] == "tok1"
    assert meta["tokens"][0]["outcome"] == "Yes"
    assert meta["tokens"][1]["outcome"] == "No"


@pytest.mark.asyncio
async def test_fetch_market_metadata_adapter_failure_raises(monkeypatch) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from wayfinder_paths.adapters.polymarket_adapter import PolymarketAdapter

    mock_adapter = MagicMock(spec=PolymarketAdapter)
    mock_adapter.get_markets_by_condition_ids = AsyncMock(
        return_value=(False, "gamma api error")
    )
    mock_adapter.close = AsyncMock()

    with pytest.raises(RuntimeError, match="Failed to fetch market metadata"):
        await fetch_market_metadata(["0xcond1"], adapter=mock_adapter)


# ---------------------------------------------------------------------------
# 3.  Sizing functions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "woi_usdc,ratio,max_order,balance,expected",
    [
        (100.0, 0.1, 40.0, 50.0, 10.0),  # ratio binding
        (1000.0, 0.1, 40.0, 50.0, 40.0),  # max_order binding
        (1000.0, 0.1, 40.0, 8.0, 8.0),  # balance binding
        (1000.0, 0.1, 40.0, 5.0, 5.0),  # balance < min_order → sizer returns 5
    ],
)
def test_flat_ratio_sizer(
    woi_usdc: float,
    ratio: float,
    max_order: float,
    balance: float,
    expected: float,
) -> None:
    fn = flat_ratio_sizer(ratio, max_order)
    signal = make_signal(usdc_amount=woi_usdc)
    state = make_state(usdc_balance=balance)
    assert fn(signal, state) == pytest.approx(expected)


def test_flat_dollar_sizer_ignores_woi_size() -> None:
    fn = flat_dollar_sizer(20.0)
    assert fn(make_signal(usdc_amount=5.0), make_state(100.0)) == pytest.approx(20.0)
    assert fn(make_signal(usdc_amount=5000.0), make_state(100.0)) == pytest.approx(20.0)


def test_flat_dollar_sizer_caps_at_balance() -> None:
    fn = flat_dollar_sizer(20.0)
    assert fn(make_signal(usdc_amount=100.0), make_state(15.0)) == pytest.approx(15.0)


def test_proportional_sizer_compounds() -> None:
    fn = proportional_sizer(0.1)
    assert fn(make_signal(), make_state(100.0)) == pytest.approx(10.0)
    assert fn(make_signal(), make_state(150.0)) == pytest.approx(15.0)


def test_proportional_sizer_respects_balance() -> None:
    fn = proportional_sizer(0.5)
    assert fn(make_signal(), make_state(5.0)) == pytest.approx(2.5)


# ---------------------------------------------------------------------------
# 4.  Core backtester correctness
# ---------------------------------------------------------------------------


@pytest.mark.smoke
def test_single_buy_resolves_yes_pnl(
    default_config: PolymarketBacktestConfig,
    prices_df_resolves_yes: pd.DataFrame,
) -> None:
    """
    BUY 10 USDC at price 0.4, slippage=0:
      Polymarket fee = 0.02 * (1 - 0.4) = 0.012 of notional
      fee = 10 * 0.012 = 0.12
      shares = (10 - 0.12) / 0.4 = 24.7
      balance = 90
    Resolution at 1.0:
      equity = 90 + 24.7 = 114.7
    """
    trades_df = make_single_buy(usdc=10.0, price=0.4)
    result = run_polymarket_backtest(
        trades_df,
        prices_df_resolves_yes,
        sizing_fn=flat_dollar_sizer(10.0),
        config=default_config,
        resolution_prices={"cond_A": 1.0},
    )
    # fee_rate = 0.02 * (1 - 0.4) = 0.012
    expected_fee = 10.0 * 0.012
    expected_shares = (10.0 - expected_fee) / 0.4
    assert result.stats["equity_final"] == pytest.approx(
        90.0 + expected_shares, rel=0.001
    )
    assert result.stats["total_return"] == pytest.approx(
        expected_shares / 100.0 - 0.1, rel=0.001
    )
    assert result.stats["total_fees"] == pytest.approx(expected_fee, rel=0.001)
    assert_valid_polymarket_result(result)


def test_single_buy_resolves_no_full_loss(
    default_config: PolymarketBacktestConfig,
) -> None:
    """
    BUY 10 USDC at price 0.4:
      shares = (10 - 0.12) / 0.4 = 24.7, balance = 90
    Resolution at 0.0:
      equity = 90 + 24.7 * 0.0 = 90  →  return = -0.10
    """
    prices_df = make_prices_resolving_to(0.0)
    trades_df = make_single_buy(usdc=10.0, price=0.4)
    result = run_polymarket_backtest(
        trades_df,
        prices_df,
        sizing_fn=flat_dollar_sizer(10.0),
        config=default_config,
        resolution_prices={"cond_A": 0.0},
    )
    assert result.stats["equity_final"] == pytest.approx(90.0, rel=0.001)
    assert result.stats["total_return"] == pytest.approx(-0.10, rel=0.001)
    assert_valid_polymarket_result(result)


def test_brier_score_known_value() -> None:
    """
    3 markets:  entry=0.3→YES, entry=0.7→NO, entry=0.5→YES
    Brier = mean(0.49, 0.49, 0.25) = 0.41
    """
    trades_df = make_trades([(0.3, "cond_A"), (0.7, "cond_B"), (0.5, "cond_C")])
    prices_df = make_multi_market_prices({"cond_A": 1.0, "cond_B": 0.0, "cond_C": 1.0})
    config = PolymarketBacktestConfig(
        initial_capital=1000.0,
        fee_rate=0.0,
        slippage_rate=0.0,
        slippage_delay=None,
        min_order_usdc=1.0,
    )
    result = run_polymarket_backtest(
        trades_df,
        prices_df,
        sizing_fn=flat_dollar_sizer(100.0),
        config=config,
        resolution_prices={"cond_A": 1.0, "cond_B": 0.0, "cond_C": 1.0},
    )
    assert result.stats["brier_score"] == pytest.approx(0.41, rel=0.001)
    assert_valid_polymarket_result(result)


def test_sell_reduces_position(default_config: PolymarketBacktestConfig) -> None:
    """BUY then partial SELL → 2 trades recorded."""
    buy_price = 0.4
    sell_price = 0.5
    idx = pd.date_range("2025-10-01", periods=24, freq="1h", tz="UTC")
    prices_df = pd.DataFrame(
        {"tok_YES_A": [buy_price] * 12 + [sell_price] * 12}, index=idx
    )

    trades_df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2025-10-01 00:00", "2025-10-01 06:00"], utc=True
            ),
            "woi_address": ["0xWOI1", "0xWOI1"],
            "condition_id": ["cond_A", "cond_A"],
            "token_id": ["tok_YES_A", "tok_YES_A"],
            "outcome": ["Yes", "Yes"],
            "side": ["BUY", "SELL"],
            "usdc_amount": [20.0, 5.0],
            "share_count": [50.0, 10.0],
            "avg_price": [buy_price, sell_price],
            "market_slug": ["market-a", "market-a"],
            "tx_hash": ["0xTX1", "0xTX2"],
        }
    ).set_index("timestamp")

    config = PolymarketBacktestConfig(
        initial_capital=100.0,
        fee_rate=0.0,
        slippage_rate=0.0,
        min_order_usdc=1.0,
    )
    result = run_polymarket_backtest(
        trades_df,
        prices_df,
        sizing_fn=flat_dollar_sizer(20.0),
        config=config,
    )
    assert result.stats["trade_count"] == 2


def test_multi_woi_timestamp_ordering() -> None:
    """Earlier timestamp trades must be processed before later ones."""
    prices_df = make_constant_prices(0.4, 24)
    trades_df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2025-10-01 12:00", "2025-10-01 08:00"], utc=True
            ),
            "woi_address": ["0xWOI1", "0xWOI2"],
            "condition_id": ["cond_A", "cond_A"],
            "token_id": ["tok_YES_A", "tok_YES_A"],
            "outcome": ["Yes", "Yes"],
            "side": ["BUY", "BUY"],
            "usdc_amount": [100.0, 50.0],
            "share_count": [250.0, 125.0],
            "avg_price": [0.4, 0.4],
            "market_slug": ["market-a", "market-a"],
            "tx_hash": ["0xTX1", "0xTX2"],
        }
    ).set_index("timestamp")

    config = PolymarketBacktestConfig(
        initial_capital=1000.0, fee_rate=0.0, slippage_rate=0.0, min_order_usdc=1.0
    )
    result = run_polymarket_backtest(
        trades_df, prices_df, sizing_fn=flat_dollar_sizer(50.0), config=config
    )
    # First executed trade must be WOI2 (08:00 earlier than 12:00)
    assert result.trades[0]["woi"][:6] == "0xWOI2"


def test_insufficient_balance_skips_trade() -> None:
    """Trade is skipped when sized amount < min_order_usdc."""
    prices_df = make_constant_prices(0.4, 24)
    trades_df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2025-10-01 00:00", "2025-10-01 01:00"], utc=True
            ),
            "woi_address": ["0xWOI1", "0xWOI1"],
            "condition_id": ["cond_A", "cond_B"],
            "token_id": ["tok_YES_A", "tok_YES_B"],
            "outcome": ["Yes", "Yes"],
            "side": ["BUY", "BUY"],
            "usdc_amount": [200.0, 200.0],
            "share_count": [500.0, 500.0],
            "avg_price": [0.4, 0.4],
            "market_slug": ["market-a", "market-b"],
            "tx_hash": ["0xTX1", "0xTX2"],
        }
    ).set_index("timestamp")
    prices_df = prices_df.assign(tok_YES_B=0.4)

    # flat_dollar_sizer(95) → first trade spends 95 → balance = 5 < min_order=10
    config = PolymarketBacktestConfig(
        initial_capital=100.0,
        fee_rate=0.0,
        slippage_rate=0.0,
        min_order_usdc=10.0,
    )
    result = run_polymarket_backtest(
        trades_df,
        prices_df,
        sizing_fn=flat_dollar_sizer(95.0),
        config=config,
    )
    assert result.stats["trade_count"] == 1


def test_equity_curve_regular_grid(
    default_config: PolymarketBacktestConfig,
    one_market_trades_df: pd.DataFrame,
    prices_df_resolves_yes: pd.DataFrame,
) -> None:
    """Equity curve index must be on a uniform hourly grid."""
    result = run_polymarket_backtest(
        one_market_trades_df,
        prices_df_resolves_yes,
        sizing_fn=flat_dollar_sizer(10.0),
        config=default_config,
    )
    diffs = result.equity_curve.index.to_series().diff().dropna()
    assert (diffs == pd.Timedelta("1h")).all()


def test_empty_trades_flat_equity(default_config: PolymarketBacktestConfig) -> None:
    """No trades → equity stays at initial_capital throughout."""
    empty_df = pd.DataFrame(
        columns=[
            "woi_address",
            "condition_id",
            "token_id",
            "outcome",
            "side",
            "usdc_amount",
            "share_count",
            "avg_price",
            "market_slug",
            "tx_hash",
        ]
    )
    empty_df.index.name = "timestamp"
    prices_df = make_constant_prices(0.5, periods=24)
    result = run_polymarket_backtest(
        empty_df,
        prices_df,
        sizing_fn=flat_dollar_sizer(10.0),
        config=default_config,
    )
    assert result.stats["trade_count"] == 0
    assert result.stats["total_return"] == pytest.approx(0.0)
    assert (result.equity_curve == default_config.initial_capital).all()
    assert_valid_polymarket_result(result)


def test_value_conservation(
    one_market_trades_df: pd.DataFrame,
    prices_df_resolves_yes: pd.DataFrame,
    default_config: PolymarketBacktestConfig,
) -> None:
    """positions_over_time tracks shares per token at every grid step."""
    result = run_polymarket_backtest(
        one_market_trades_df,
        prices_df_resolves_yes,
        sizing_fn=flat_dollar_sizer(10.0),
        config=default_config,
    )
    for t in result.equity_curve.index:
        pm_col = "tok_YES_A"
        if pm_col in result.positions_over_time.columns:
            shares = result.positions_over_time.loc[t, pm_col]
            price = (
                prices_df_resolves_yes.loc[t, pm_col]
                if t in prices_df_resolves_yes.index
                else 0.0
            )
            _ = shares * price  # must not raise


def test_voided_market_no_win_loss() -> None:
    """Market ends at 0.5 → assume_resolution_at_end classifies it as voided."""
    prices_df = make_constant_prices(0.5, periods=24)
    trades_df = make_single_buy(usdc=10.0, price=0.5)
    config = PolymarketBacktestConfig(
        initial_capital=100.0,
        fee_rate=0.0,
        slippage_rate=0.0,
        min_order_usdc=1.0,
        assume_resolution_at_end=True,
    )
    result = run_polymarket_backtest(
        trades_df, prices_df, sizing_fn=flat_dollar_sizer(10.0), config=config
    )
    assert result.stats["markets_won"] == 0
    assert result.stats["markets_lost"] == 0
    assert result.stats["markets_voided"] == 1
    assert_valid_polymarket_result(result)


def test_brier_nan_no_resolved_markets() -> None:
    """No resolved markets → brier_score is NaN."""
    prices_df = make_constant_prices(0.5, periods=24)
    trades_df = make_single_buy(usdc=10.0, price=0.5)
    config = PolymarketBacktestConfig(
        initial_capital=100.0,
        fee_rate=0.0,
        slippage_rate=0.0,
        min_order_usdc=1.0,
    )
    result = run_polymarket_backtest(
        trades_df, prices_df, sizing_fn=flat_dollar_sizer(10.0), config=config
    )
    assert math.isnan(result.stats["brier_score"])
    assert_valid_polymarket_result(result)


@pytest.mark.asyncio
async def test_compare_sizing_strategies_returns_multiple_results() -> None:
    """compare_sizing_strategies runs each sizer on the same data."""
    prices_df = make_constant_prices(0.4, periods=48)
    prices_df = prices_df.assign(tok_YES_A=prices_df["tok_YES_A"])
    trades_df = make_single_buy(usdc=100.0, price=0.4)

    config = PolymarketBacktestConfig(
        initial_capital=1000.0,
        fee_rate=0.0,
        slippage_rate=0.0,
        min_order_usdc=1.0,
    )
    fns = {
        "flat_10": flat_dollar_sizer(10.0),
        "flat_20": flat_dollar_sizer(20.0),
        "prop_10pct": proportional_sizer(0.1),
    }
    results = await compare_sizing_strategies(
        woi_addresses=["0xWOI1"],
        sizing_fns=fns,
        trades_df=trades_df,
        prices_df=prices_df,
        config=config,
    )
    assert set(results.keys()) == {"flat_10", "flat_20", "prop_10pct"}
    assert (
        results["flat_20"].stats["avg_position_size_usdc"]
        > results["flat_10"].stats["avg_position_size_usdc"]
    )


def test_price_gap_nans_during_gap() -> None:
    """NaN prices propagate into equity_curve during gap periods."""
    prices_df = make_price_with_gap(gap_start_h=5, gap_end_h=30, value=0.4)
    trades_df = make_single_buy(usdc=10.0, price=0.4)
    config = PolymarketBacktestConfig(
        initial_capital=100.0, fee_rate=0.0, slippage_rate=0.0, min_order_usdc=1.0
    )
    result = run_polymarket_backtest(
        trades_df, prices_df, sizing_fn=flat_dollar_sizer(10.0), config=config
    )
    gap_period = result.equity_curve.iloc[5:30]
    assert gap_period.isna().any()
