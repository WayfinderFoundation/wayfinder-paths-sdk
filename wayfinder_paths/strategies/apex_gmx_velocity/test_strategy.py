"""Smoke tests for ApexGmxVelocityStrategy.

These verify:
  1. Strategy class loads and ref/signal/decide are wired
  2. compute_signal produces shape-correct, leverage-bounded weights
  3. The signal applied through the backtester reproduces the
     audited performance ranges (Sharpe 60d ∈ [3.0, 5.0])
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pandas as pd
import pytest

from wayfinder_paths.strategies.apex_gmx_velocity.signal import compute_signal
from wayfinder_paths.strategies.apex_gmx_velocity.strategy import (
    ApexGmxVelocityStrategy,
)


async def _fetch_hl_prices(days: int = 200) -> pd.DataFrame:
    now = datetime.now(timezone.utc)
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
    assert p["entry_z"] == 2.0
    assert p["target_leverage"] == 1.5


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
    assert (abs_sum <= target_lev + 1e-9).all(), \
        f"weights exceed target_leverage; max sum={abs_sum.max()}"
    # When entered, both legs are equal-magnitude (dollar-neutral)
    nonzero = targets[targets.abs().sum(axis=1) > 0]
    if not nonzero.empty:
        assert ((nonzero["APEX"].abs() - nonzero["GMX"].abs()).abs() < 1e-9).all(), \
            "APEX and GMX legs not equal magnitude when entered"


@pytest.mark.smoke
def test_backtest_reproduces_ref():
    """Re-run the audit's 60d window and confirm Sharpe is within the
    expected band declared in examples.json."""
    import json
    from pathlib import Path

    fixture = json.loads(
        (Path(__file__).parent / "examples.json").read_text()
    )
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
    assert expected["sharpe_60d_min"] <= sh <= expected["sharpe_60d_max"], \
        f"60d sharpe {sh:.2f} outside expected [{expected['sharpe_60d_min']}, " \
        f"{expected['sharpe_60d_max']}]"
    assert expected["trade_count_60d_min"] <= n <= expected["trade_count_60d_max"], \
        f"60d trade count {n} outside expected " \
        f"[{expected['trade_count_60d_min']}, {expected['trade_count_60d_max']}]"
