"""Event-driven copy-trade backtester for Polymarket.

Replays historical WOI trade signals against a price series and simulates
our sized positions, fees, and final equity.

Usage:
    result = run_polymarket_backtest(
        trades_df, prices_df,
        sizing_fn=flat_dollar_sizer(20.0),
        config=PolymarketBacktestConfig(fee_rate=0.02),
        resolution_prices={"cond_A": 1.0, "cond_B": 0.0},
    )
    print(f"Return: {result.stats['total_return']:.2%}")
    print(f"Brier:  {result.stats['brier_score']:.3f}")
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from wayfinder_paths.core.backtesting.polymarket_data import detect_resolutions
from wayfinder_paths.core.backtesting.polymarket_types import (
    PolymarketBacktestConfig,
    PolymarketBacktestResult,
    PolymarketBacktestState,
    PolymarketBacktestStats,
    SizingFn,
)
from wayfinder_paths.strategies.polymarket_copy_strategy.parser import TradeSignal


def run_polymarket_backtest(
    trades_df: pd.DataFrame,
    prices_df: pd.DataFrame,
    sizing_fn: SizingFn,
    config: PolymarketBacktestConfig | None = None,
    resolution_prices: dict[str, float] | None = None,
) -> PolymarketBacktestResult:
    """Run an event-driven Polymarket copy-trade backtest.

    Args:
        trades_df: WOI historical trades indexed by timestamp.
            Required columns: woi_address, condition_id, token_id, outcome,
            side, usdc_amount, share_count, avg_price, market_slug.
        prices_df: Regular-grid price series (columns = token_ids).
        sizing_fn: (signal, state) → our USDC allocation per trade.
        config: Simulation parameters (fees, slippage, min order, …).
        resolution_prices: {condition_id: 0.0 or 1.0} — overrides auto-detect.
            Used for stats (brier, markets_won/lost/voided) but NOT for equity.
            Equity is always valued at live prices from prices_df.

    Returns:
        PolymarketBacktestResult with equity_curve, stats, trades, positions_over_time.
    """
    if config is None:
        config = PolymarketBacktestConfig()

    # Sort signals chronologically
    trades_df = trades_df.sort_index()

    # Group signals by timestamp for O(1) lookup per grid step
    trade_groups: dict[Any, list[pd.Series]] = {}
    for ts, row in trades_df.iterrows():
        trade_groups.setdefault(ts, []).append(row)

    # State
    usdc_balance = config.initial_capital
    positions: dict[str, float] = {}  # token_id → shares held
    token_to_cond: dict[str, str] = {}  # token_id → condition_id
    # condition_id → avg_price of first BUY (used for Brier)
    entry_price_by_cond: dict[str, float] = {}

    all_trades: list[dict[str, Any]] = []
    equity_values: list[float] = []
    position_snapshots: list[dict[str, float]] = []
    total_fees = 0.0

    for ts in prices_df.index:
        current_prices: pd.Series = prices_df.loc[ts]

        # --- Execute any signals at this timestamp ---
        for row in trade_groups.get(ts, []):
            signal = TradeSignal(
                woi_address=str(row["woi_address"]),
                condition_id=str(row["condition_id"]),
                token_id=str(row["token_id"]),
                outcome=str(row["outcome"]),
                side=str(row["side"]),  # type: ignore[arg-type]
                usdc_amount=float(row["usdc_amount"]),
                share_count=float(row["share_count"]),
                avg_price=float(row["avg_price"]),
                market_slug=str(row.get("market_slug", "")),
                dedupe_key=str(row.get("tx_hash", "")),
            )

            state = PolymarketBacktestState(
                usdc_balance=usdc_balance,
                positions=dict(positions),
            )
            sized_usdc = sizing_fn(signal, state)

            if sized_usdc < config.min_order_usdc:
                continue

            price = signal.avg_price
            if price <= 0:
                continue

            if signal.side == "BUY":
                fee = sized_usdc * (config.fee_rate + config.slippage_rate)
                shares = (sized_usdc - fee) / price
                usdc_balance -= sized_usdc
                positions[signal.token_id] = positions.get(signal.token_id, 0.0) + shares
                total_fees += fee
                token_to_cond[signal.token_id] = signal.condition_id
                if signal.condition_id not in entry_price_by_cond:
                    entry_price_by_cond[signal.condition_id] = price
                all_trades.append(
                    {
                        "timestamp": ts,
                        "woi": signal.woi_address,
                        "condition_id": signal.condition_id,
                        "token_id": signal.token_id,
                        "side": "BUY",
                        "usdc_amount": sized_usdc,
                        "shares": shares,
                        "price": price,
                        "fee": fee,
                    }
                )

            elif signal.side == "SELL":
                shares_held = positions.get(signal.token_id, 0.0)
                if shares_held <= 0:
                    continue
                shares_to_sell = min(sized_usdc / price, shares_held)
                gross = shares_to_sell * price
                fee = gross * (config.fee_rate + config.slippage_rate)
                net = gross - fee
                usdc_balance += net
                positions[signal.token_id] = shares_held - shares_to_sell
                total_fees += fee
                all_trades.append(
                    {
                        "timestamp": ts,
                        "woi": signal.woi_address,
                        "condition_id": signal.condition_id,
                        "token_id": signal.token_id,
                        "side": "SELL",
                        "usdc_amount": net,
                        "shares": -shares_to_sell,
                        "price": price,
                        "fee": fee,
                    }
                )

        # --- Value portfolio at current prices ---
        position_value = 0.0
        has_nan_price = False
        for token_id, shares in positions.items():
            if shares <= 0:
                continue
            if token_id not in current_prices.index:
                has_nan_price = True
                break
            p = float(current_prices[token_id])
            if math.isnan(p):
                has_nan_price = True
                break
            position_value += shares * p

        equity = float("nan") if has_nan_price else usdc_balance + position_value
        equity_values.append(equity)
        position_snapshots.append(dict(positions))

    equity_curve = pd.Series(equity_values, index=prices_df.index, dtype=float)
    positions_over_time = pd.DataFrame(position_snapshots, index=prices_df.index)

    stats = _compute_stats(
        equity_curve=equity_curve,
        trades=all_trades,
        total_fees=total_fees,
        config=config,
        entry_price_by_cond=entry_price_by_cond,
        token_to_cond=token_to_cond,
        positions=positions,
        prices_df=prices_df,
        resolution_prices=resolution_prices,
    )

    return PolymarketBacktestResult(
        equity_curve=equity_curve,
        stats=stats,
        trades=all_trades,
        positions_over_time=positions_over_time,
    )


def _compute_stats(
    *,
    equity_curve: pd.Series,
    trades: list[dict[str, Any]],
    total_fees: float,
    config: PolymarketBacktestConfig,
    entry_price_by_cond: dict[str, float],
    token_to_cond: dict[str, str],
    positions: dict[str, float],  # final positions
    prices_df: pd.DataFrame,
    resolution_prices: dict[str, float] | None,
) -> PolymarketBacktestStats:
    equity_final = float(equity_curve.dropna().iloc[-1]) if equity_curve.dropna().size > 0 else config.initial_capital
    total_return = (equity_final - config.initial_capital) / config.initial_capital

    trade_count = len(trades)
    buy_trades = [t for t in trades if t["side"] == "BUY"]
    avg_position_size_usdc = (
        float(np.mean([t["usdc_amount"] for t in buy_trades])) if buy_trades else 0.0
    )

    # Resolve each traded condition to won/lost/voided
    cond_resolutions = _resolve_conditions(
        conds_traded=set(entry_price_by_cond.keys()),
        token_to_cond=token_to_cond,
        positions=positions,
        prices_df=prices_df,
        resolution_prices=resolution_prices,
        config=config,
    )

    markets_won = sum(1 for v in cond_resolutions.values() if v == 1.0)
    markets_lost = sum(1 for v in cond_resolutions.values() if v == 0.0)
    markets_voided = sum(1 for v in cond_resolutions.values() if v is None)
    markets_traded = len(cond_resolutions)

    resolved_conds = {c: v for c, v in cond_resolutions.items() if v is not None}
    if resolved_conds:
        brier_vals = [
            (v - entry_price_by_cond[c]) ** 2
            for c, v in resolved_conds.items()
            if c in entry_price_by_cond
        ]
        brier_score = float(np.mean(brier_vals)) if brier_vals else float("nan")
    else:
        brier_score = float("nan")

    total_resolved = markets_won + markets_lost
    market_win_rate = markets_won / total_resolved if total_resolved > 0 else float("nan")

    return PolymarketBacktestStats(
        equity_final=equity_final,
        total_return=total_return,
        total_fees=total_fees,
        trade_count=trade_count,
        avg_position_size_usdc=avg_position_size_usdc,
        markets_traded=markets_traded,
        markets_won=markets_won,
        markets_lost=markets_lost,
        markets_voided=markets_voided,
        market_win_rate=market_win_rate,
        brier_score=brier_score,
    )


def _resolve_conditions(
    *,
    conds_traded: set[str],
    token_to_cond: dict[str, str],
    positions: dict[str, float],
    prices_df: pd.DataFrame,
    resolution_prices: dict[str, float] | None,
    config: PolymarketBacktestConfig,
) -> dict[str, float | None]:
    """Return {cond_id: 1.0 | 0.0 | None} for every traded condition.

    Priority:
    1. Explicit resolution_prices (keyed by condition_id).
    2. Auto-detect from prices_df last value when assume_resolution_at_end=True.
    3. Otherwise voided (None).
    """
    result: dict[str, float | None] = {}
    for cond_id in conds_traded:
        if resolution_prices and cond_id in resolution_prices:
            res = resolution_prices[cond_id]
            # Map to clean 1.0 / 0.0 / None
            if res >= config.resolution_threshold:
                result[cond_id] = 1.0
            elif res <= (1.0 - config.resolution_threshold):
                result[cond_id] = 0.0
            else:
                result[cond_id] = None
        elif config.assume_resolution_at_end:
            # Look up the token for this condition and read its last price
            token_id = next(
                (tok for tok, cond in token_to_cond.items() if cond == cond_id), None
            )
            if token_id and token_id in prices_df.columns:
                last_price = float(prices_df[token_id].dropna().iloc[-1])
                if last_price >= config.resolution_threshold:
                    result[cond_id] = 1.0
                elif last_price <= (1.0 - config.resolution_threshold):
                    result[cond_id] = 0.0
                else:
                    result[cond_id] = None  # voided
            else:
                result[cond_id] = None
        else:
            result[cond_id] = None

    return result


async def compare_sizing_strategies(
    woi_addresses: list[str],
    sizing_fns: dict[str, SizingFn],
    config: PolymarketBacktestConfig | None = None,
    trades_df: pd.DataFrame | None = None,
    prices_df: pd.DataFrame | None = None,
    resolution_prices: dict[str, float] | None = None,
) -> dict[str, PolymarketBacktestResult]:
    """Run each sizing strategy on the same data and return a keyed results dict.

    When trades_df and prices_df are provided they are used directly (testing /
    offline mode). Otherwise, data is fetched via Delta Lab — which is not yet
    implemented.
    """
    if config is None:
        config = PolymarketBacktestConfig()

    if trades_df is None or prices_df is None:
        raise NotImplementedError(
            "compare_sizing_strategies requires Delta Lab data integration. "
            "Inject trades_df and prices_df directly for offline testing."
        )

    return {
        name: run_polymarket_backtest(
            trades_df,
            prices_df,
            fn,
            config,
            resolution_prices=resolution_prices,
        )
        for name, fn in sizing_fns.items()
    }
