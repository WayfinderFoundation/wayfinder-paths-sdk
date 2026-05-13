"""Smoke tests for ApexGmxVelocityStrategy.

These verify:
  1. Strategy class loads and ref/signal/decide are wired
  2. compute_signal produces shape-correct, leverage-bounded weights
  3. The signal applied through the backtester reproduces the
     audited performance ranges (Sharpe 60d ∈ [3.0, 5.0])
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pandas as pd
import pytest

from wayfinder_paths.core.strategies.active_perps_testing import (
    assert_active_perps_backtest_runs,
    assert_active_perps_reproduces_ref,
)
from wayfinder_paths.strategies.apex_gmx_velocity.signal import compute_signal
from wayfinder_paths.strategies.apex_gmx_velocity.strategy import (
    ApexGmxVelocityStrategy,
)
from wayfinder_paths.tests.test_utils import load_strategy_examples


async def _fetch_hl_prices(
    days: int = 200,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> pd.DataFrame:
    if start_ms is None or end_ms is None:
        now = datetime.now(UTC)
        start_ms = int((now - timedelta(days=days)).timestamp() * 1000)
        end_ms = int((now + timedelta(hours=1)).timestamp() * 1000)
    series = {}
    async with httpx.AsyncClient(timeout=20.0) as c:
        for sym in ["APEX", "GMX"]:
            r = await c.post(
                "https://api.hyperliquid.xyz/info",
                json={
                    "type": "candleSnapshot",
                    "req": {
                        "coin": sym,
                        "interval": "1h",
                        "startTime": start_ms,
                        "endTime": end_ms,
                    },
                },
            )
            r.raise_for_status()
            df = pd.DataFrame(r.json())
            df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
            df = df.set_index("t")
            series[sym] = df["c"].astype(float).rename(sym)
    return pd.concat([series["APEX"], series["GMX"]], axis=1).dropna().sort_index()


@pytest.mark.smoke
def test_class_wires():
    """REF/SIGNAL/DECIDE wiring sanity."""
    cls = ApexGmxVelocityStrategy
    assert cls.SIGNAL.endswith(":compute_signal")
    assert cls.DECIDE.endswith(":decide")
    assert cls.REF.exists(), "backtest_ref.json missing"
    p = cls.DEFAULT_PARAMS
    assert set(p["symbols"]) == {"APEX", "GMX"}
    assert p["lookback_bars"] == 72
    assert p["entry_z"] == 0.75
    assert p["target_leverage"] == 2.5


@pytest.mark.smoke
def test_signal_invariants():
    """Signal output is shape-correct and leverage-bounded."""
    prices = asyncio.run(_fetch_hl_prices(days=14))
    assert {"APEX", "GMX"} <= set(prices.columns)
    sf = compute_signal(prices, None, ApexGmxVelocityStrategy.DEFAULT_PARAMS)
    targets = sf.targets
    assert list(targets.columns) == list(prices.columns)
    abs_sum = targets.abs().sum(axis=1)
    target_lev = ApexGmxVelocityStrategy.DEFAULT_PARAMS["target_leverage"]
    assert (abs_sum <= target_lev + 1e-9).all(), (
        f"weights exceed target_leverage; max sum={abs_sum.max()}"
    )
    # When entered, both legs are equal-magnitude (dollar-neutral)
    nonzero = targets[targets.abs().sum(axis=1) > 0]
    if not nonzero.empty:
        assert ((nonzero["APEX"].abs() - nonzero["GMX"].abs()).abs() < 1e-9).all(), (
            "APEX and GMX legs not equal magnitude when entered"
        )


@pytest.mark.smoke
def test_backtest_reproduces_ref():
    """Re-run the audit's 60d window and confirm Sharpe is within the
    expected band declared in examples.json."""
    fixture = load_strategy_examples(Path(__file__))
    expected = fixture["expected_backtest_ranges"]

    from wayfinder_paths.core.backtesting.backtester import run_backtest
    from wayfinder_paths.core.backtesting.types import BacktestConfig

    prices = asyncio.run(_fetch_hl_prices(days=200))
    cutoff = prices.index[-1] - pd.Timedelta(days=60)
    sub = prices[prices.index >= cutoff]
    sf = compute_signal(sub, None, ApexGmxVelocityStrategy.DEFAULT_PARAMS)
    cfg = BacktestConfig(
        fee_rate=0.00045,
        slippage_rate=0.0025,
        leverage=ApexGmxVelocityStrategy.DEFAULT_PARAMS["target_leverage"],
        initial_capital=1.0,
        funding_rates=None,
        enable_liquidation=True,
        periods_per_year=8760,
        rebalance_threshold=0.02,
    )
    r = run_backtest(sub, sf.targets, cfg)
    sh = float(r.stats["sharpe"])
    n = int(r.stats.get("trade_count", 0))
    assert expected["sharpe_60d_min"] <= sh <= expected["sharpe_60d_max"], (
        f"60d sharpe {sh:.2f} outside expected [{expected['sharpe_60d_min']}, "
        f"{expected['sharpe_60d_max']}]"
    )
    assert expected["trade_count_60d_min"] <= n <= expected["trade_count_60d_max"], (
        f"60d trade count {n} outside expected "
        f"[{expected['trade_count_60d_min']}, {expected['trade_count_60d_max']}]"
    )


@pytest.mark.smoke
def test_trigger_backtest_divergence_check():
    """Drive signal+decide through `backtest_perps_trigger` (same code path as
    live `_run_trigger`) over `SMOKE_TEST_WINDOW_DAYS`. Catches live↔backtest
    divergence bugs (side-channel NAV reads, framework state writes, purity
    violations) AND signal/decide regressions that produce a negative
    total_return on the smoke window.
    """
    days = ApexGmxVelocityStrategy.SMOKE_TEST_WINDOW_DAYS
    prices = asyncio.run(_fetch_hl_prices(days=days))
    asyncio.run(
        assert_active_perps_backtest_runs(
            ApexGmxVelocityStrategy,
            prices,
            expect_trades=True,
        )
    )


@pytest.mark.ref_reproduction
def test_reproduces_backtest_ref():
    """Slow: re-run the trigger backtest over the exact `ref.data.window` and
    assert stats match `ref.performance` within tolerance. Fails on any
    signal/decide/data drift since the ref was bonded.

    Uses the framework's `fetch_prices` (Delta Lab → Hyperliquid fallback),
    same code path as bonding. Fetching directly from HL candles produces
    different coverage and prices and will not reproduce the ref.
    """
    from wayfinder_paths.core.backtesting.data import (
        align_dataframes,
        fetch_funding_rates,
        fetch_prices,
    )
    from wayfinder_paths.core.backtesting.ref import load_ref

    ref = load_ref(ApexGmxVelocityStrategy.REF.parent)

    async def _fetch_window():
        prices = await fetch_prices(
            ref.data.symbols,
            ref.data.window.start,
            ref.data.window.end,
            ref.data.interval,
        )
        try:
            funding = await fetch_funding_rates(
                ref.data.symbols, ref.data.window.start, ref.data.window.end
            )
            prices, funding = await align_dataframes(prices, funding, method="ffill")
        except (ValueError, KeyError):
            funding = None
        return prices, funding

    prices, funding = asyncio.run(_fetch_window())
    asyncio.run(
        assert_active_perps_reproduces_ref(ApexGmxVelocityStrategy, prices, funding)
    )
