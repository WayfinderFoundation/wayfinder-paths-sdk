"""Exhaustive expected-utility oracle over the genome space.

Truth definition: a genome's oracle utility is the MEAN utility over the
world's HIDDEN continuations (common random numbers — every genome sees the
same paths), never its score on one lucky realized path. U*, the ε-optimal
set, and per-genome rankings all derive from that expectation.

Evaluation semantics (the shared contract with compiler.py's interpreter):
- signal/filter series from the SAME library builders, full-frame vectorized
- entry: signal∧filter true at bar t (flat) → fill open[t+1]
- exits: completed-bar conditions referenced to the DECISION close; fill at
  next open; one position per symbol; fees fee_bps per side on notional
- engine parity is asserted on net USD PnL (`parity_check`); utility is
  computed uniformly by THIS module for all genomes (internal consistency is
  what ranking requires), with equity marked at fills — a documented, mild
  understatement of intra-trade downside vs the engine's per-bar marking.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from wayfinder_paths.jobs.benchmarks.grammar import FILTERS, Genome
from wayfinder_paths.jobs.signal_library import SIGNAL_LIBRARY

_SIGNALS = {s.name: s for s in SIGNAL_LIBRARY}
INITIAL_EQUITY = 10_000.0
DEFAULT_MAX_HOLD = 96  # safety cap: no genome may hold forever


@dataclass
class ContinuationSeries:
    """Precomputed per-continuation arrays shared by every genome."""

    opens: np.ndarray
    closes: np.ndarray
    hours: np.ndarray
    signals: dict[str, np.ndarray]
    filters: dict[str, np.ndarray]
    vol_size: np.ndarray  # per-bar vol_target size multiplier


def prepare_continuation(
    rows: Sequence[Mapping[str, Any]],
    *,
    signal_names: Sequence[str],
) -> ContinuationSeries:
    frame = pd.DataFrame(rows)
    closes = frame["close"].to_numpy(dtype=float)
    opens = frame["open"].to_numpy(dtype=float)
    hours = pd.to_datetime(frame["timestamp"]).dt.hour.to_numpy(dtype=int)

    signals = {
        name: _SIGNALS[name].build(frame).fillna(False).to_numpy(dtype=bool)
        for name in signal_names
    }

    close_series = frame["close"]
    sma50 = close_series.rolling(50).mean()
    tr = (frame["high"] - frame["low"]).rolling(14).mean()
    # Median over the trailing engine compute window (512 bars), not the full
    # frame — the interpreter sees a bounded view, and the contract must match.
    tr_median_series = tr.rolling(512, min_periods=16).median()
    filters: dict[str, np.ndarray] = {}
    for name in FILTERS:
        if name == "none":
            filters[name] = np.ones(len(frame), dtype=bool)
        elif name == "above_sma50":
            filters[name] = (close_series > sma50).fillna(False).to_numpy(dtype=bool)
        elif name == "below_sma50":
            filters[name] = (close_series < sma50).fillna(False).to_numpy(dtype=bool)
        elif name == "high_vol":
            filters[name] = (tr > tr_median_series).fillna(False).to_numpy(dtype=bool)
        elif name == "low_vol":
            filters[name] = (tr <= tr_median_series).fillna(False).to_numpy(dtype=bool)
        elif name.startswith("session_"):
            bucket = {"session_a": 0, "session_b": 1, "session_c": 2}[name]
            filters[name] = (hours // 8) == bucket

    returns = close_series.pct_change().rolling(20).std()
    vol = returns.to_numpy(dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        size = np.clip(0.01 / vol, 0.25, 4.0)
    filters_nan = ~np.isfinite(size)
    size[filters_nan] = 1.0
    return ContinuationSeries(
        opens=opens,
        closes=closes,
        hours=hours,
        signals=signals,
        filters=filters,
        vol_size=size,
    )


def evaluate_genome(
    genome: Genome,
    series: ContinuationSeries,
    *,
    fee_bps: float = 4.5,
    max_hold: int = DEFAULT_MAX_HOLD,
) -> dict[str, Any]:
    """One genome on one continuation → trade PnLs + equity-at-fill path."""
    exit_params = dict(genome.exit_params)
    sizing_params = dict(genome.sizing_params)
    sign = 1.0 if genome.direction == "long" else -1.0
    fee_rate = fee_bps / 10_000.0
    opens, closes = series.opens, series.closes
    entry_mask = series.signals[genome.signal] & series.filters[genome.confirm_filter]
    candidates = np.flatnonzero(entry_mask)
    n = len(closes)

    pnls: list[float] = []
    exit_indices: list[int] = []
    cursor = 0
    for t in candidates:
        if t < cursor or t + 1 >= n:
            continue
        entry_ref = closes[t]  # decision close: target/stop distances
        entry_fill = opens[t + 1]
        size = 1.0
        if genome.sizing_family == "vol_target":
            target = float(sizing_params.get("target_vol") or 0.01)
            size = float(np.clip(series.vol_size[t] * (target / 0.01), 0.25, 4.0))
        exit_bar = None
        peak = entry_ref
        hold_cap = int(exit_params.get("hold_bars") or max_hold)
        for held, bar in enumerate(range(t + 1, min(t + 1 + max_hold, n - 1)), start=1):
            close = closes[bar]
            move = sign * (close / entry_ref - 1.0)
            peak = max(peak, close) if sign > 0 else min(peak, close)
            trail_move = -(close / peak - 1.0) if sign > 0 else (close / peak - 1.0)
            family = genome.exit_family
            if family == "fixed_time":
                if held >= hold_cap:
                    exit_bar = bar
            elif family == "target_stop":
                if move >= float(exit_params["target_pct"]) or move <= -float(
                    exit_params["stop_pct"]
                ):
                    exit_bar = bar
            elif family == "trailing":
                if (sign > 0 and trail_move <= -float(exit_params["trail_pct"])) or (
                    sign < 0 and -trail_move <= -float(exit_params["trail_pct"])
                ):
                    exit_bar = bar
            elif family == "time_stop":
                if held >= hold_cap or move <= -float(exit_params["stop_pct"]):
                    exit_bar = bar
            if exit_bar is not None:
                break
        if exit_bar is None:
            exit_bar = min(t + max_hold, n - 2)
        exit_fill = opens[exit_bar + 1]
        pnl = size * sign * (exit_fill - entry_fill) - fee_rate * size * (
            entry_fill + exit_fill
        )
        pnls.append(float(pnl))
        exit_indices.append(exit_bar + 1)
        cursor = exit_bar + 1

    return {"pnls": pnls, "exit_indices": exit_indices}


def utility_from_trades(
    pnls: Sequence[float],
    *,
    weights: Mapping[str, float],
    initial_equity: float = INITIAL_EQUITY,
) -> float:
    """Constitution-shaped utility on the equity-at-fill path. Uniform for
    every genome — internal consistency is what oracle ranking requires."""
    if not pnls:
        return 0.0
    equity = initial_equity + np.cumsum(np.asarray(pnls, dtype=float))
    if np.any(equity <= 0):
        return -10.0  # ruin: hard floor far below any feasible utility
    log_steps = np.diff(np.log(np.concatenate([[initial_equity], equity])))
    growth = float(log_steps.sum())
    negatives = log_steps[log_steps < 0]
    downside = (
        float(np.sqrt(np.mean(negatives**2))) if len(negatives) else 0.0
    )
    worst_k = max(1, len(pnls) // 10)
    tail = float(abs(np.sort(np.asarray(pnls))[:worst_k].sum())) / initial_equity
    fees_proxy = 0.0  # fees are inside pnls; turnover term deferred to v1
    return (
        growth
        - float(weights.get("downside", 0.0)) * downside
        - float(weights.get("tail", 0.0)) * tail
        - float(weights.get("turnover", 0.0)) * fees_proxy
    )


def evaluate_space(
    genomes: Sequence[Genome],
    continuations: Sequence[ContinuationSeries],
    *,
    weights: Mapping[str, float],
    fee_bps: float = 4.5,
) -> dict[str, Any]:
    """The oracle: expected utility per genome over all hidden continuations,
    U*, null utility, ε-optimal set, and basin labels."""
    expected: dict[str, float] = {}
    basins: dict[str, str] = {}
    for genome in genomes:
        utilities = [
            utility_from_trades(
                evaluate_genome(genome, series, fee_bps=fee_bps)["pnls"],
                weights=weights,
            )
            for series in continuations
        ]
        expected[genome.genome_id] = float(np.mean(utilities))
        # Basin approximation: the structural neighborhood that local moves
        # explore — signal x direction x filter.
        basins[genome.genome_id] = (
            f"{genome.signal}|{genome.direction}|{genome.confirm_filter}"
        )
    u_star = max(expected.values())
    u_null = 0.0  # the no-trade genome-equivalent
    epsilon = max(0.02 * abs(u_star - u_null), 1e-4)
    optimal_set = {gid for gid, value in expected.items() if value >= u_star - epsilon}
    best_id = max(expected, key=expected.get)  # type: ignore[arg-type]
    return {
        "expected_utility": expected,
        "u_star": u_star,
        "u_null": u_null,
        "epsilon": epsilon,
        "epsilon_optimal": sorted(optimal_set),
        "best_genome_id": best_id,
        "best_basin": basins[best_id],
        "basins": basins,
    }
