"""backtest_perps_trigger: trigger-pattern backtest for ActivePerpsStrategy.

The driver walks bars, runs `decide(ctx)` per bar, applies fills at next-bar open,
and returns a `BacktestResult` whose shape is identical to `quick_backtest`.

`signal_fn(prices, funding, params)` is precomputed once over the full window
(vectorized — fast). `decide(ctx)` is called per bar with a `TriggerContext` that
exposes handlers, signal-at-now, and free-form state.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

import numpy as np
import pandas as pd

from wayfinder_paths.core.backtesting.data import (
    align_dataframes,
    fetch_funding_rates,
    fetch_prices,
)
from wayfinder_paths.core.backtesting.stats import calculate_stats
from wayfinder_paths.core.backtesting.types import BacktestResult, BacktestStats
from wayfinder_paths.core.perps.context import SignalFrame, TriggerContext
from wayfinder_paths.core.perps.handlers.backtest import BacktestHandler, purity_sandbox
from wayfinder_paths.core.perps.state import StateStore

INTERVAL_PERIODS = {
    "1m": 365 * 24 * 60,
    "5m": 365 * 24 * 12,
    "15m": 365 * 24 * 4,
    "1h": 365 * 24,
    "4h": 365 * 6,
    "1d": 365,
}


SignalFn = Callable[
    [pd.DataFrame, pd.DataFrame | None, dict[str, Any]], "SignalFrame | pd.DataFrame"
]
DecideFn = Callable[[TriggerContext], Awaitable[None]]


async def default_decide(ctx: TriggerContext) -> None:
    """Default: interpret signal value as target signed size in base units (perp venue)."""
    target = ctx.signal_at_now()
    current = await ctx.perp.get_positions()
    min_usd = float(ctx.params.get("min_order_usd", 10.0))
    for sym, tgt_size in target.items():
        cur_size = current[sym].size if sym in current else 0.0
        diff = float(tgt_size) - cur_size
        if diff == 0:
            continue
        notional = abs(diff) * ctx.perp.mid(sym)
        if notional < min_usd:
            continue
        side = "buy" if diff > 0 else "sell"
        # reduce-only when crossing toward flat in the opposite direction
        reduce = (
            (cur_size != 0)
            and (np.sign(cur_size) != np.sign(diff))
            and (abs(diff) <= abs(cur_size))
        )
        await ctx.perp.place_order(sym, side, abs(diff), "market", reduce_only=reduce)


def _normalize_signal(
    out: Any, index: pd.DatetimeIndex, symbols: list[str]
) -> SignalFrame:
    if isinstance(out, SignalFrame):
        return out
    if isinstance(out, pd.DataFrame):
        df = out.reindex(index=index, columns=symbols).fillna(0.0)
        return SignalFrame(targets=df)
    raise TypeError(
        f"signal_fn must return SignalFrame or pd.DataFrame, got {type(out).__name__}"
    )


async def backtest_perps_trigger(
    *,
    signal_fn: SignalFn,
    decide_fn: DecideFn | None = None,
    symbols: list[str],
    start: str,
    end: str,
    interval: str = "1h",
    hip3_dexes: list[str] | None = None,
    params: dict[str, Any] | None = None,
    fill_model: str = "next_bar_open",
    slippage_bps: float = 1.0,
    fee_bps: float = 4.5,
    min_order_usd: float = 10.0,
    initial_capital: float = 10_000.0,
    leverage: float = 1.0,
    include_funding: bool = True,
    prices: pd.DataFrame | None = None,
    funding: pd.DataFrame | None = None,
) -> BacktestResult:
    """Run a trigger-pattern perps backtest.

    Provide either (`symbols`, `start`, `end`) and let the driver fetch data,
    or pass `prices`/`funding` directly for tests / offline replays.

    `signal_fn` is called once with the full price + funding frames; output is
    a target-size DataFrame (or SignalFrame). `decide_fn(ctx)` runs per bar.
    """
    if fill_model not in ("next_bar_open", "same_bar"):
        raise NotImplementedError(f"fill_model={fill_model!r} not implemented")

    hip3_dexes = list(hip3_dexes or [])
    params = dict(params or {})
    params.setdefault("min_order_usd", min_order_usd)

    if prices is None:
        prices = await fetch_prices(symbols, start, end, interval)
    if funding is None and include_funding:
        try:
            funding = await fetch_funding_rates(symbols, start, end)
            prices, funding = await align_dataframes(prices, funding, method="ffill")
        except (ValueError, KeyError):
            funding = None

    if not isinstance(prices.index, pd.DatetimeIndex):
        prices.index = pd.to_datetime(prices.index)

    prices = prices[symbols].copy()
    if funding is not None:
        funding = (
            funding.reindex(index=prices.index, columns=symbols).ffill().fillna(0.0)
        )

    # Precompute signal frame.
    raw_signal = signal_fn(prices, funding, params)
    signal_frame = _normalize_signal(raw_signal, prices.index, symbols)

    # Build handlers — primary perp + one per hip3 dex (data shared for now; per-dex
    # data wiring is a Phase 7+ concern).
    perp = BacktestHandler(
        "perp", prices, funding, slippage_bps, fee_bps, min_order_usd
    )
    hip3 = {
        dex: BacktestHandler(
            f"hip3:{dex}", prices, funding, slippage_bps, fee_bps, min_order_usd
        )
        for dex in hip3_dexes
    }

    state = StateStore("__backtest__", "backtest")
    decide = decide_fn or default_decide
    if not inspect.iscoroutinefunction(decide):
        raise TypeError("decide_fn must be an async function")

    # Per-bar accumulators.
    n = len(prices.index)
    equity = np.full(n, np.nan, dtype=float)
    turnover = np.zeros(n)
    cost_series = np.zeros(n)
    fee_series = np.zeros(n)
    funding_series = np.zeros(n)
    realized_series = np.zeros(n)
    pnl_series = np.zeros(n)
    cash = float(initial_capital)
    nav = float(cash)
    # Make the cost knobs visible to opt-in sizing helpers via params.
    params.setdefault("fee_bps", fee_bps)
    params.setdefault("slippage_bps", slippage_bps)
    prev_total_value: dict[str, float] = {h.venue: 0.0 for h in [perp, *hip3.values()]}
    positions_history: list[dict[str, float]] = []
    trades: list[dict[str, Any]] = []

    def _record_fills(fills, t):
        for f in fills:
            if f.ok:
                trades.append(
                    {
                        "timestamp": t,
                        "venue": f.venue,
                        "symbol": f.symbol,
                        "side": f.side,
                        "size": f.fill_size,
                        "price": f.fill_price,
                        "fee": f.fee_paid,
                        "order_type": f.order_type,
                        "reduce_only": f.reduce_only,
                    }
                )

    for i, t in enumerate(prices.index):
        # 1) advance handlers; apply pending fills (next-bar-open mode only — at the new bar's price).
        for h in [perp, *hip3.values()]:
            h.set_bar(i)
            if fill_model == "next_bar_open":
                _record_fills(h.apply_pending_fills(), t)

        # 2) expose pre-trade NAV to decide. Match legacy: NAV is measured BEFORE
        #    this bar's funding accrual (funding is debited after the trade loop).
        unrealized_pre = sum(h.mark_to_market_value() for h in [perp, *hip3.values()])
        bar_costs_pre = sum(
            h._bar_fees - h._bar_realized_pnl  # noqa: SLF001
            for h in [perp, *hip3.values()]
        )
        nav_pre = float(cash) + float(unrealized_pre) - bar_costs_pre
        state.set("nav", nav_pre)

        # 4) build context and run decide.
        ctx = TriggerContext(
            perp=perp,
            hip3=hip3,
            params=params,
            state=state,
            signal=signal_frame,
            t=t.to_pydatetime(),
        )
        with purity_sandbox():
            await decide(ctx)

        all_handlers = [perp, *hip3.values()]

        # 5) same-bar fills happen at this bar's price, after decide.
        if fill_model == "same_bar":
            for h in all_handlers:
                _record_fills(h.apply_pending_fills(), t)

        # 6) accrue funding on the post-trade book — matches legacy ordering.
        for h in all_handlers:
            h.accrue_funding()

        # 4) snapshot per-bar metrics.
        bar_fees = 0.0
        bar_funding = 0.0
        bar_realized = 0.0
        bar_turnover = 0.0
        for h in [perp, *hip3.values()]:
            f, fnd, rl = h.consume_bar_costs()
            bar_fees += f
            bar_funding += fnd
            bar_realized += rl
            bar_turnover += h.gross_notional()

        # 5) NAV and pnl accounting.
        unrealized = sum(h.mark_to_market_value() for h in [perp, *hip3.values()])
        cash += bar_realized + bar_funding - bar_fees
        nav = float(cash) + float(unrealized)
        equity[i] = nav

        fee_series[i] = bar_fees
        funding_series[
            i
        ] = (
            -bar_funding
        )  # convention: negative = income (matches quick_backtest's total_funding)
        cost_series[i] = bar_fees
        turnover[i] = bar_turnover / max(initial_capital, 1.0)
        realized_series[i] = bar_realized

        # snapshot positions across all venues for positions_over_time
        snap: dict[str, float] = {}
        for h in [perp, *hip3.values()]:
            for sym, sz in h._positions.items():  # noqa: SLF001 — internal access for reporting
                if sz != 0:
                    snap[f"{h.venue}:{sym}"] = sz
        positions_history.append(snap)

    # Normalize equity to start at 1.0 — `calculate_stats` computes total_return as
    # `equity_final - 1.0`, matching `quick_backtest`'s default `initial_capital=1.0`.
    raw_equity = pd.Series(equity, index=prices.index, name="raw_equity")
    equity_series = (raw_equity / float(initial_capital)).rename("equity")
    returns = equity_series.pct_change().fillna(0.0)
    pnl_series = equity_series.diff().fillna(0.0).to_numpy()

    positions_over_time = pd.DataFrame(positions_history, index=prices.index).fillna(
        0.0
    )

    metrics_by_period = pd.DataFrame(
        {
            "equity": equity_series.values,
            "turnover": turnover,
            "cost": cost_series,
            "fees": fee_series,
            "funding": funding_series,
            "realized_pnl": realized_series,
            "pnl": pnl_series,
        },
        index=prices.index,
    )

    periods_per_year = INTERVAL_PERIODS.get(interval, 365 * 24)
    stats: BacktestStats = calculate_stats(
        returns=returns,
        equity_curve=equity_series,
        trades=trades,
        turnover_series=turnover.tolist(),
        cost_series=cost_series.tolist(),
        fee_series=fee_series.tolist(),
        funding_series=funding_series.tolist(),
        periods_per_year=periods_per_year,
        prices=prices,
    )

    return BacktestResult(
        equity_curve=equity_series,
        returns=returns,
        stats=stats,
        trades=trades,
        metrics_by_period=metrics_by_period,
        positions_over_time=positions_over_time,
        liquidated=False,
        liquidation_timestamp=None,
    )
