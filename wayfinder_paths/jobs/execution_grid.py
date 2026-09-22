"""Passive-entry mechanism grid: the cheap inner loop on the execution side.

The origin of the one starter that passes the screen was not the signal
(a 4 bps ten-minute bounce) but the mechanism that monetized it — a
resting bid below the close, a passive target, a wide stop, a short hold —
found by sweeping a few thousand execution rows in a fast harness before one
was implemented in the engine (``scripts/research_maker_backtest.py``). This
module is that sweep without numba: one signal, one symbol, every row of
offset × TTL × target × hold × stop, ranked by the smaller of the train and
validation Sharpe. It is a screen, never evidence: the chosen row is
implemented in the real engine and certified there.

Semantics mirror the harness and the engine's resting-order model:

- a completed signal bar rests a post-only limit at ``close ∓ offset·ATR``
  that is live for ``ttl`` following bars;
- it fills only when a later bar trades through the price by the
  trade-through margin, at the limit, paying the maker fee;
- the stop is fill-relative (``stop·ATR`` at the fill) and has same-bar
  precedence over the passive target; the target (``target·ATR``) is a
  passive reduce-only order that needs its own trade-through;
- after ``hold`` bars without either, the position exits at the next open
  as a taker; stop and time exits pay the taker fee plus slippage;
- one position at a time; a resting order that never fills blocks new
  signals until it expires.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from wayfinder_paths.jobs.indicators import atr as wilder_atr

DEFAULT_GRID: dict[str, tuple[float, ...]] = {
    "entry_offset_atr": (0.5, 0.75, 1.0, 1.5, 2.0),
    "entry_ttl_bars": (1, 2, 3),
    "target_atr": (0.5, 0.75, 1.0, 1.5, 2.0),
    "hold_bars": (4, 6, 8, 12, 18),
    "stop_atr": (2.0, 3.0, 4.0, 5.0, 6.0),
}
GRID_FIELDS = tuple(DEFAULT_GRID)
SECONDS_PER_YEAR = 365 * 24 * 3600


@dataclass(frozen=True)
class GridCosts:
    maker_fee_bps: float = 1.5
    taker_fee_bps: float = 4.5
    slippage_bps: float = 3.5
    trade_through_bps: float = 1.0


@dataclass(frozen=True)
class _Row:
    offset: float
    ttl: int
    target: float
    hold: int
    stop: float


def grid_rows(grid: Mapping[str, Sequence[float]] | None = None) -> list[_Row]:
    table = {**DEFAULT_GRID, **dict(grid or {})}
    return [
        _Row(float(offset), int(ttl), float(target), int(hold), float(stop))
        for offset, ttl, target, hold, stop in itertools.product(
            *(table[field] for field in GRID_FIELDS)
        )
    ]


def _segment_stats(returns: np.ndarray, *, bars_per_year: float) -> dict[str, float]:
    """Per-bar return path to return, annualized Sharpe and drawdown."""
    if len(returns) == 0:
        return {"return": 0.0, "sharpe": 0.0, "max_drawdown": 0.0}
    equity = np.cumprod(1.0 + returns)
    peak = np.maximum.accumulate(equity)
    drawdown = float(np.min(equity / peak - 1.0))
    std = float(returns.std())
    sharpe = float(returns.mean() / std * math.sqrt(bars_per_year)) if std > 0 else 0.0
    return {
        "return": float(equity[-1] - 1.0),
        "sharpe": sharpe,
        "max_drawdown": drawdown,
    }


class _Market:
    """One symbol's bars as arrays, with the signal and ATR aligned."""

    def __init__(
        self,
        bars: pd.DataFrame,
        signal: Sequence[bool] | np.ndarray | pd.Series,
        *,
        side: str,
        atr_period: int,
    ) -> None:
        if side not in {"long", "short"}:
            raise ValueError("side must be long or short")
        self.side = side
        self.sign = 1.0 if side == "long" else -1.0
        self.open = bars["open"].astype(float).to_numpy()
        self.high = bars["high"].astype(float).to_numpy()
        self.low = bars["low"].astype(float).to_numpy()
        self.close = bars["close"].astype(float).to_numpy()
        self.atr = wilder_atr(bars, atr_period).to_numpy(dtype=float)
        flags = np.asarray(pd.Series(signal).fillna(False).astype(bool).to_numpy())
        if len(flags) != len(self.close):
            raise ValueError("signal must be row-aligned with the bars")
        warm = np.zeros(len(flags), dtype=bool)
        warm[atr_period + 1 :] = True
        self.signals = np.flatnonzero(flags & warm & np.isfinite(self.atr))
        self.n = len(self.close)
        # The adverse side for a long is the low (fills, stops); the favorable
        # side is the high (targets). A short mirrors them.
        self.adverse = self.low if side == "long" else self.high
        self.favorable = self.high if side == "long" else self.low


def _simulate_row(
    market: _Market, row: _Row, costs: GridCosts, *, start: int, end: int
) -> tuple[np.ndarray, int, int, int]:
    """Bar-return path over [start, end) plus (accepted signals, fills, wins).

    Outcomes are computed for every signal bar in one vectorized pass, then
    resolved sequentially so only one position (or one resting order) is
    live at a time."""
    sign = market.sign
    tt = costs.trade_through_bps / 1e4
    maker = costs.maker_fee_bps / 1e4
    taker = costs.taker_fee_bps / 1e4
    slip = costs.slippage_bps / 1e4
    signals = market.signals[(market.signals >= start) & (market.signals < end - 1)]
    returns = np.zeros(end - start)
    if len(signals) == 0:
        return returns, 0, 0, 0
    limit = market.close[signals] - sign * row.offset * market.atr[signals]
    # Fill bar: the first of the ttl following bars whose adverse side trades
    # through the limit. Bars past the window never fill.
    fill_bar = np.full(len(signals), -1, dtype=int)
    threshold = limit * (1.0 - sign * tt)
    for k in range(1, row.ttl + 1):
        bar = signals + k
        valid = (fill_bar < 0) & (bar < end)
        touched = np.zeros(len(signals), dtype=bool)
        touched[valid] = sign * (market.adverse[bar[valid]] - threshold[valid]) <= 0.0
        fill_bar[touched] = bar[touched]
    filled = fill_bar >= 0
    entry = limit
    entry_atr = np.where(filled, market.atr[np.maximum(fill_bar - 1, 0)], np.nan)
    stop = entry - sign * row.stop * entry_atr
    target = entry + sign * row.target * entry_atr
    # Exit resolution over the holding window: stop first on any bar
    # (including the fill bar), then the passive target from the bar after
    # the fill, else a taker exit at the open after ``hold`` bars.
    offsets = np.arange(row.hold)
    bars = fill_bar[:, None] + offsets[None, :]
    inside = filled[:, None] & (bars < end)
    safe = np.clip(bars, 0, market.n - 1)
    stop_hit = inside & (sign * (market.adverse[safe] - stop[:, None]) <= 0.0)
    target_hit = (
        inside
        & (offsets[None, :] >= 1)
        & (sign * (market.favorable[safe] - target[:, None] * (1.0 + sign * tt)) >= 0.0)
    )
    any_hit = stop_hit | target_hit
    first = np.argmax(any_hit, axis=1)
    hit = any_hit[np.arange(len(signals)), first]
    exit_bar = np.where(hit, fill_bar + first, fill_bar + row.hold)
    exit_kind = np.where(
        hit, np.where(stop_hit[np.arange(len(signals)), first], 1, 2), 3
    )  # 1 stop, 2 target, 3 time
    exit_bar = np.minimum(exit_bar, end - 1)
    open_at_exit = market.open[np.clip(exit_bar, 0, market.n - 1)]
    stop_price = (
        np.minimum(stop, open_at_exit) if sign > 0 else np.maximum(stop, open_at_exit)
    ) * (1.0 - sign * slip)
    time_price = open_at_exit * (1.0 - sign * slip)
    exit_price = np.where(
        exit_kind == 1, stop_price, np.where(exit_kind == 2, target, time_price)
    )
    exit_fee = np.where(exit_kind == 2, maker, taker)
    gross = sign * (exit_price / entry - 1.0)
    net = gross - maker - exit_fee
    # Sequential resolution: one live order or position at a time.
    accepted = fills = wins = 0
    position: int = start
    index = 0
    order = np.argsort(signals, kind="stable")
    while index < len(order):
        candidate = int(order[index])
        signal_bar = int(signals[candidate])
        if signal_bar < position:
            index += 1
            continue
        accepted += 1
        if not filled[candidate]:
            position = signal_bar + row.ttl + 1
            index += 1
            continue
        fills += 1
        fill = int(fill_bar[candidate])
        out = int(exit_bar[candidate])
        # Mark to market as position value over the entry notional: a fixed
        # quantity gains sign·(P − E)/E, so a short from 100 to 80 is +20%,
        # not the compounded negation of the long's bar returns (+22%). The
        # fill bar carries the move from the entry to its close, each holding
        # bar the close-to-close move, the exit bar the move to the exit
        # price; fees come off the equity at the fill and at the exit.
        entry_price = float(entry[candidate])
        exit_at = float(exit_price[candidate])
        path = (
            np.concatenate(([entry_price], market.close[fill:out], [exit_at]))
            if out > fill
            else np.array([entry_price, exit_at])
        )
        value = 1.0 + sign * (path / entry_price - 1.0)
        bar_returns = value[1:] / value[:-1] - 1.0
        bar_returns[0] -= maker / value[0]
        bar_returns[-1] -= (
            float(exit_fee[candidate]) * exit_at / entry_price / value[-2]
        )
        span = min(len(bar_returns), end - fill)
        returns[fill - start : fill - start + span] += bar_returns[:span]
        if net[candidate] > 0:
            wins += 1
        position = out
        index += 1
    return returns, accepted, fills, wins


def passive_entry_grid(
    bars: pd.DataFrame,
    signal: Sequence[bool] | np.ndarray | pd.Series,
    *,
    side: str = "long",
    bar_seconds: int,
    grid: Mapping[str, Sequence[float]] | None = None,
    costs: GridCosts = GridCosts(),
    split: tuple[float, float] = (0.70, 0.15),
    min_trades: int = 50,
    max_drawdown: float = -0.25,
    atr_period: int = 14,
    top: int = 10,
) -> dict[str, Any]:
    """Sweep every grid row on the first ``split[0]`` of the bars, keep the
    rows with positive train Sharpe, at least ``min_trades`` fills and a
    drawdown inside the bound, score the survivors on the next ``split[1]``
    and rank by the smaller Sharpe. The remaining tail is never read."""
    market = _Market(bars, signal, side=side, atr_period=atr_period)
    train_end = int(market.n * float(split[0]))
    validation_end = min(market.n, int(market.n * float(split[0] + split[1])))
    if train_end < atr_period + 10 or validation_end <= train_end + 2:
        raise ValueError("not enough bars for a train/validation split")
    bars_per_year = SECONDS_PER_YEAR / float(bar_seconds)
    rows = grid_rows(grid)
    scored: list[dict[str, Any]] = []
    viable = 0
    for row in rows:
        returns, accepted, fills, wins = _simulate_row(
            market, row, costs, start=0, end=train_end
        )
        train = _segment_stats(returns, bars_per_year=bars_per_year)
        train.update(
            {
                "trades": fills,
                "signals": accepted,
                "fill_rate": round(fills / accepted, 4) if accepted else 0.0,
                "win_rate": round(wins / fills, 4) if fills else 0.0,
            }
        )
        if not (
            train["sharpe"] > 0
            and fills >= min_trades
            and train["max_drawdown"] > max_drawdown
        ):
            continue
        viable += 1
        v_returns, v_accepted, v_fills, v_wins = _simulate_row(
            market, row, costs, start=train_end, end=validation_end
        )
        validation = _segment_stats(v_returns, bars_per_year=bars_per_year)
        validation.update(
            {
                "trades": v_fills,
                "signals": v_accepted,
                "fill_rate": round(v_fills / v_accepted, 4) if v_accepted else 0.0,
                "win_rate": round(v_wins / v_fills, 4) if v_fills else 0.0,
            }
        )
        scored.append(
            {
                "entry_offset_atr": row.offset,
                "entry_ttl_bars": row.ttl,
                "target_atr": row.target,
                "hold_bars": row.hold,
                "stop_atr": row.stop,
                "train": {k: round(float(v), 4) for k, v in train.items()},
                "validation": {k: round(float(v), 4) for k, v in validation.items()},
                "score": round(min(train["sharpe"], validation["sharpe"]), 4),
            }
        )
    scored.sort(key=lambda item: -float(item["score"]))
    return {
        "side": side,
        "evaluated": len(rows),
        "viable": viable,
        "signals": int(len(market.signals)),
        "bars": int(market.n),
        "split": {
            "train_bars": train_end,
            "validation_bars": validation_end - train_end,
        },
        "filters": {
            "train_sharpe": "> 0",
            "min_trades": min_trades,
            "max_drawdown": max_drawdown,
        },
        "costs": {
            "maker_fee_bps": costs.maker_fee_bps,
            "taker_fee_bps": costs.taker_fee_bps,
            "slippage_bps": costs.slippage_bps,
            "trade_through_bps": costs.trade_through_bps,
        },
        "ranking": "min(train_sharpe, validation_sharpe)",
        "top": scored[: max(1, int(top))],
    }
