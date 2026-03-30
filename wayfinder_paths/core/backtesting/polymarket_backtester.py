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

from wayfinder_paths.adapters.polymarket_adapter.fees import polymarket_fee_rate
from wayfinder_paths.core.backtesting.polymarket_parser import TradeSignal
from wayfinder_paths.core.backtesting.polymarket_types import (
    PolymarketBacktestConfig,
    PolymarketBacktestResult,
    PolymarketBacktestState,
    PolymarketBacktestStats,
    SizingFn,
)


def _execution_price(
    signal_price: float,
    next_price: float | None,
    side: str,
    config: PolymarketBacktestConfig,
) -> float:
    """Compute the effective execution price after modelling copy-trade delay.

    If slippage_delay is set and the next grid price is worse for us,
    blend toward it:  exec = (1-d)*signal + d*next.
    If the next price is better (or unavailable), fall back to flat
    slippage_rate applied to the signal price.
    """
    if config.slippage_delay is not None and next_price is not None:
        worse_for_buyer = next_price > signal_price
        worse_for_seller = next_price < signal_price
        if (side == "BUY" and worse_for_buyer) or (side == "SELL" and worse_for_seller):
            d = config.slippage_delay
            return (1.0 - d) * signal_price + d * next_price

    # Fallback: flat slippage (always makes price worse)
    if side == "BUY":
        return signal_price * (1.0 + config.slippage_rate)
    return signal_price * (1.0 - config.slippage_rate)


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

    # Assign each trade to the first grid point STRICTLY AFTER its timestamp.
    # A trade at time T is processed at the smallest grid time t where t > T.
    # This guarantees no lookahead: at step t the trade is known (T < t) and
    # prices_df[t] contains only observations from before t.
    grid_freq = pd.tseries.frequencies.to_offset(config.equity_interval)
    one_period = pd.tseries.frequencies.to_offset(config.equity_interval)
    trade_groups: dict[Any, list[pd.Series]] = {}
    grid_start = prices_df.index[0] if len(prices_df.index) > 0 else None
    grid_end = prices_df.index[-1] if len(prices_df.index) > 0 else None

    for ts, row in trades_df.iterrows():
        snapped = ts.ceil(grid_freq)  # type: ignore[arg-type]
        # ceil maps exact boundary to itself — push to next period so
        # the trade is never processed in the same instant it occurred.
        if snapped == ts:
            snapped += one_period  # type: ignore[operator]
        # Drop trades that fall outside the price grid
        if grid_start is None or snapped > grid_end:  # type: ignore[operator]
            continue
        if snapped < grid_start:
            snapped = grid_start
        trade_groups.setdefault(snapped, []).append(row)

    # Precompute next-row prices for slippage delay model.
    # next_prices_map[ts][token_id] = price at the grid step after ts.
    grid_index = prices_df.index
    next_prices_map: dict[Any, pd.Series] = {}
    for i in range(len(grid_index) - 1):
        next_prices_map[grid_index[i]] = prices_df.iloc[i + 1]

    # State
    usdc_balance = config.initial_capital
    positions: dict[str, float] = {}  # token_id → shares held
    token_to_cond: dict[str, str] = {}  # token_id → condition_id
    # condition_id → avg_price of first BUY (used for Brier)
    entry_price_by_cond: dict[str, float] = {}
    # token_id → weighted-average entry price (used for stop-loss)
    entry_price_by_token: dict[str, float] = {}

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

            if signal.avg_price <= 0:
                continue

            # Model copy-trade execution delay
            next_row = next_prices_map.get(ts)
            next_p: float | None = None
            if next_row is not None and signal.token_id in next_row.index:
                val = float(next_row[signal.token_id])
                if not math.isnan(val):
                    next_p = val
            price = _execution_price(
                signal.avg_price,
                next_p,
                signal.side,
                config,
            )

            if signal.side == "BUY":
                pm_fee_rate = polymarket_fee_rate(price, "BUY")
                fee = sized_usdc * pm_fee_rate
                shares = (sized_usdc - fee) / price
                usdc_balance -= sized_usdc
                # Update weighted-average entry price
                old_shares = positions.get(signal.token_id, 0.0)
                old_entry = entry_price_by_token.get(signal.token_id, 0.0)
                new_shares = old_shares + shares
                entry_price_by_token[signal.token_id] = (
                    old_shares * old_entry + shares * price
                ) / new_shares
                positions[signal.token_id] = new_shares
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
                    if not config.copy_sells:
                        continue
                    # copy_sells=True but we don't hold this token — skip
                    continue

                if config.copy_sells:
                    # Sell our entire position when WOI sells
                    shares_to_sell = shares_held
                else:
                    shares_to_sell = min(sized_usdc / price, shares_held)

                gross = shares_to_sell * price
                pm_fee_rate = polymarket_fee_rate(price, "SELL")
                fee = gross * pm_fee_rate
                net = gross - fee
                usdc_balance += net
                positions[signal.token_id] = shares_held - shares_to_sell
                if positions[signal.token_id] <= 0:
                    entry_price_by_token.pop(signal.token_id, None)
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

        # --- Stop-loss check ---
        if config.stop_loss_pct is not None:
            for token_id in list(positions):
                shares = positions[token_id]
                if shares <= 0:
                    continue
                entry_p = entry_price_by_token.get(token_id)
                if entry_p is None or entry_p <= 0:
                    continue
                if token_id not in current_prices.index:
                    continue
                cur_p = float(current_prices[token_id])
                if math.isnan(cur_p):
                    continue
                loss_pct = (entry_p - cur_p) / entry_p
                if loss_pct >= config.stop_loss_pct:
                    gross = shares * cur_p
                    pm_fee_rate = polymarket_fee_rate(cur_p, "SELL")
                    fee = gross * pm_fee_rate
                    net = gross - fee
                    usdc_balance += net
                    positions[token_id] = 0.0
                    entry_price_by_token.pop(token_id, None)
                    total_fees += fee
                    all_trades.append(
                        {
                            "timestamp": ts,
                            "woi": "",
                            "condition_id": token_to_cond.get(token_id, ""),
                            "token_id": token_id,
                            "side": "STOP_LOSS",
                            "usdc_amount": net,
                            "shares": -shares,
                            "price": cur_p,
                            "fee": fee,
                        }
                    )

        # --- Auto-redeem resolved markets (no fee) ---
        for token_id in list(positions):
            shares = positions[token_id]
            if shares <= 0:
                continue
            if token_id not in current_prices.index:
                continue
            p = float(current_prices[token_id])
            if math.isnan(p):
                continue
            if p >= config.resolution_threshold:
                # Resolved YES — redeem shares at $1.00
                usdc_balance += shares * 1.0
                positions[token_id] = 0.0
                all_trades.append(
                    {
                        "timestamp": ts,
                        "woi": "",
                        "condition_id": token_to_cond.get(token_id, ""),
                        "token_id": token_id,
                        "side": "REDEEM_YES",
                        "usdc_amount": shares * 1.0,
                        "shares": -shares,
                        "price": 1.0,
                        "fee": 0.0,
                    }
                )
            elif p <= (1.0 - config.resolution_threshold):
                # Resolved NO — shares worth $0, position zeroed
                positions[token_id] = 0.0
                all_trades.append(
                    {
                        "timestamp": ts,
                        "woi": "",
                        "condition_id": token_to_cond.get(token_id, ""),
                        "token_id": token_id,
                        "side": "REDEEM_NO",
                        "usdc_amount": 0.0,
                        "shares": -shares,
                        "price": 0.0,
                        "fee": 0.0,
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
    equity_final = (
        float(equity_curve.dropna().iloc[-1])
        if equity_curve.dropna().size > 0
        else config.initial_capital
    )
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
    market_win_rate = (
        markets_won / total_resolved if total_resolved > 0 else float("nan")
    )

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
