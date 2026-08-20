"""Research-only passive-fill sweep for maker starter candidates.

Run with an ephemeral numba dependency so it does not become an SDK runtime
requirement::

    uv run --no-project --python 3.12 --with numpy --with pandas \
      --with duckdb --with pytz --with numba \
      python scripts/research_maker_backtest.py DATA.parquet

The final starter evidence must still be reproduced through jobs_v1.  This
script is the fast breadth screen: completed-bar signals, next-bar passive
orders, strict trade-through fills, maker fees, taker stops/time exits, and
conservative same-bar stop precedence.
"""

from __future__ import annotations

import argparse
import itertools
import math
from collections.abc import Iterable

import duckdb
import numpy as np
import pandas as pd
from numba import njit, prange


@njit
def _simulate(  # noqa: PLR0913
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    rsi: np.ndarray,
    atr: np.ndarray,
    start: int,
    end: int,
    params: np.ndarray,
    staged: bool,
) -> tuple[float, float, float, int, float]:
    (
        offset,
        ttl,
        target_1,
        target_2,
        fraction_1,
        hold,
        stop_multiple,
        risk_budget,
    ) = params
    maker_rate = 0.00015
    taker_rate = 0.00045
    taker_slippage = 0.00035
    trade_through = 0.0001

    balance = 1.0
    quantity = 0.0
    entry = stop = take_1 = take_2 = 0.0
    fill_index = -1
    stage = 0
    pending = False
    pending_price = 0.0
    pending_until = -1
    market_exit = -1
    previous_equity = peak_equity = 1.0
    return_sum = squared_return_sum = 0.0
    return_count = 0
    max_drawdown = 0.0
    trades = wins = 0
    trade_start_balance = 0.0

    for index in range(start, end):
        if quantity > 0 and market_exit == index:
            exit_price = opens[index] * (1 - taker_slippage)
            balance += quantity * (exit_price - entry)
            balance -= quantity * exit_price * taker_rate
            quantity = 0.0
            market_exit = -1
            trades += 1
            if balance > trade_start_balance:
                wins += 1

        just_filled = False
        if quantity == 0 and pending:
            if index > pending_until:
                pending = False
            elif lows[index] <= pending_price * (1 - trade_through):
                entry = pending_price
                trade_start_balance = balance
                signal_atr = atr[index - 1]
                stop_fraction = stop_multiple * signal_atr / entry
                allocation = min(1.0, risk_budget / stop_fraction)
                quantity = balance * allocation / entry
                balance -= quantity * entry * maker_rate
                stop = entry - stop_multiple * signal_atr
                take_1 = entry + target_1 * signal_atr
                take_2 = entry + target_2 * signal_atr
                fill_index = index
                stage = 0
                pending = False
                just_filled = True

        if quantity > 0:
            if lows[index] <= stop:
                exit_price = min(stop, opens[index]) * (1 - taker_slippage)
                balance += quantity * (exit_price - entry)
                balance -= quantity * exit_price * taker_rate
                quantity = 0.0
                market_exit = -1
                trades += 1
                if balance > trade_start_balance:
                    wins += 1
            elif not just_filled:
                if staged:
                    if stage == 0 and highs[index] >= take_1 * (1 + trade_through):
                        closed = quantity * fraction_1
                        balance += closed * (take_1 - entry)
                        balance -= closed * take_1 * maker_rate
                        quantity -= closed
                        stage = 1
                    if (
                        quantity > 0
                        and stage == 1
                        and highs[index] >= take_2 * (1 + trade_through)
                    ):
                        balance += quantity * (take_2 - entry)
                        balance -= quantity * take_2 * maker_rate
                        quantity = 0.0
                        trades += 1
                        if balance > trade_start_balance:
                            wins += 1
                elif highs[index] >= take_1 * (1 + trade_through):
                    balance += quantity * (take_1 - entry)
                    balance -= quantity * take_1 * maker_rate
                    quantity = 0.0
                    trades += 1
                    if balance > trade_start_balance:
                        wins += 1

            if (
                quantity > 0
                and market_exit < 0
                and index - fill_index >= int(hold) - 1
                and index + 1 < end
            ):
                market_exit = index + 1

        # The completed bar at index owns the signal.  Its passive bid first
        # becomes executable during index + 1, never on the signal candle.
        if (
            quantity == 0
            and not pending
            and index + 1 < end
            and np.isfinite(rsi[index])
            and np.isfinite(atr[index])
            and rsi[index] <= 30.0
        ):
            pending_price = closes[index] - offset * atr[index]
            pending_until = index + int(ttl)
            pending = True

        equity = balance + (quantity * (closes[index] - entry) if quantity > 0 else 0)
        period_return = equity / previous_equity - 1 if previous_equity > 0 else 0.0
        return_sum += period_return
        squared_return_sum += period_return * period_return
        return_count += 1
        previous_equity = equity
        peak_equity = max(peak_equity, equity)
        max_drawdown = min(max_drawdown, equity / peak_equity - 1)

    if quantity > 0:
        exit_price = closes[end - 1] * (1 - taker_slippage)
        balance += quantity * (exit_price - entry)
        balance -= quantity * exit_price * taker_rate
        trades += 1
        if balance > trade_start_balance:
            wins += 1

    mean_return = return_sum / return_count
    variance = max(squared_return_sum / return_count - mean_return * mean_return, 0.0)
    sharpe = (
        mean_return / math.sqrt(variance) * math.sqrt(365 * 24 * 12)
        if variance > 0
        else 0.0
    )
    return balance - 1.0, sharpe, max_drawdown, trades, wins / max(trades, 1)


@njit(parallel=True)
def _run_grid(  # noqa: PLR0913
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    rsi: np.ndarray,
    atr: np.ndarray,
    start: int,
    end: int,
    params: np.ndarray,
    staged: bool,
) -> np.ndarray:
    results = np.empty((len(params), 5))
    for index in prange(len(params)):
        net_return, sharpe, drawdown, trades, win_rate = _simulate(
            opens,
            highs,
            lows,
            closes,
            rsi,
            atr,
            start,
            end,
            params[index],
            staged,
        )
        results[index, 0] = net_return
        results[index, 1] = sharpe
        results[index, 2] = drawdown
        results[index, 3] = trades
        results[index, 4] = win_rate
    return results


def _features(frame: pd.DataFrame) -> tuple[np.ndarray, ...]:
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column])
    close = frame["close"]
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            (frame["high"] - frame["low"]).abs(),
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = true_range.ewm(alpha=1 / 14, adjust=False).mean()
    return tuple(
        series.to_numpy(np.float64)
        for series in (
            frame["open"],
            frame["high"],
            frame["low"],
            frame["close"],
            rsi,
            atr,
        )
    )


def _grid_rows(staged: bool, deep: bool) -> Iterable[tuple[float, ...]]:
    offsets = [0.5, 0.75, 1.0, 1.5, 2.0] if deep else [0.05, 0.1, 0.2, 0.3, 0.5]
    stops = [2.0, 3.0, 4.0, 5.0, 6.0] if deep else [1.0, 1.5, 2.0, 2.5]
    holds = [4, 6, 8, 12, 18] if deep else [2, 3, 4, 6]
    if staged:
        return itertools.product(
            offsets,
            [1, 2, 3],
            [0.5, 0.75, 1.0],
            [1.5, 2.0, 3.0],
            [0.5, 0.67],
            holds,
            stops,
            [0.005, 0.0075, 0.01, 0.015, 10.0],
        )
    return itertools.product(
        offsets,
        [1, 2, 3],
        [0.5, 0.75, 1.0, 1.5, 2.0],
        [0.0],
        [1.0],
        holds,
        stops,
        [0.005, 0.0075, 0.01, 0.015, 10.0],
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset")
    parser.add_argument("--symbol", default="HYPE")
    parser.add_argument("--deep", action="store_true")
    args = parser.parse_args()
    frame = (
        duckdb.connect()
        .execute(
            "SELECT * FROM read_parquet(?) WHERE symbol=? ORDER BY timestamp",
            [args.dataset, args.symbol],
        )
        .df()
    )
    arrays = _features(frame)
    train_end = int(len(frame) * 0.70)
    validation_end = int(len(frame) * 0.85)

    for name, staged in (("full", False), ("staged", True)):
        params = np.array(list(_grid_rows(staged, args.deep)), dtype=np.float64)
        training = _run_grid(*arrays, 220, train_end, params, staged)
        viable = np.where(
            (training[:, 1] > 0) & (training[:, 3] >= 50) & (training[:, 2] > -0.25)
        )[0]
        top = viable[np.argsort(training[viable, 1])[-50:][::-1]]
        scored = []
        for index in top:
            validation = _simulate(
                *arrays, train_end, validation_end, params[index], staged
            )
            full = _simulate(*arrays, 220, len(frame), params[index], staged)
            scored.append(
                (min(training[index, 1], validation[1]), index, validation, full)
            )
        print(name, "grid", len(params), "viable", len(viable))
        for _, index, validation, full in sorted(scored, reverse=True)[:15]:
            print(
                name,
                "params",
                params[index].tolist(),
                "train",
                np.round(training[index], 4).tolist(),
                "validation",
                np.round(np.asarray(validation), 4).tolist(),
                "full",
                np.round(np.asarray(full), 4).tolist(),
            )


if __name__ == "__main__":
    main()
