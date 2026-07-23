"""Per-trade price-path forensics: what the chart shows that PnL rows hide.

A closed-trade row says what a trade *made*; it says nothing about the path —
how far price moved against the position during the hold (MAE), how much
favorable move existed (MFE), and what happened in the bars right AFTER the
exit. Those are exactly the facts a human reads off the chart when judging an
exit rule ("the stop-out kept falling for 20 more minutes"), and exactly what
the intervention agent could not see. This module computes them
deterministically for both backtest and forward trades.

Honesty contract: single-trade counterfactuals are HYPOTHESIS FUEL, not
evidence. `aggregate_trade_forensics` over the backtest population is where an
exit tweak gets its first real test; the experiments grid + walk-forward
adjudicate before any proposal.

All excursions are in bps of the ENTRY price and are direction-aware:
positive = in the trade's favor.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import pandas as pd

POST_BARS = (4, 8, 16)
STOP_GRID = (0.02, 0.025, 0.03, 0.035)
# The engine's bracket fills carry no intent metadata — a close without an
# exit_reason IS the protective stop (strategy closes always label themselves).
BRACKET_EXIT_REASON = "bracket_stop"


def _bps(value: float, entry_price: float) -> float:
    return round(value / entry_price * 1e4, 1)


def position_side_of_close(close_fill_side: str) -> str:
    """A reduce-only fill's side is the EXIT side: buying closes a short."""
    return "short" if str(close_fill_side).lower() == "buy" else "long"


def match_entry_fill(
    fills: Iterable[Mapping[str, Any]],
    *,
    symbol: str,
    exit_ts: pd.Timestamp,
) -> Mapping[str, Any] | None:
    """Latest non-reduce-only fill for the symbol strictly before the exit.

    Valid under the one-position-per-symbol model jobs_v1 strategies use; a
    partial-fill ladder would need explicit trade ids instead.
    """
    best: Mapping[str, Any] | None = None
    best_ts: pd.Timestamp | None = None
    for fill in fills:
        if str(fill.get("symbol")) != symbol or fill.get("reduce_only"):
            continue
        ts = pd.Timestamp(str(fill.get("timestamp")))
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        if ts >= exit_ts:
            continue
        if best_ts is None or ts > best_ts:
            best, best_ts = fill, ts
    return best


def _closing_fill_reason(
    fills: Iterable[Mapping[str, Any]],
    *,
    symbol: str,
    exit_ts: pd.Timestamp,
) -> str | None:
    for fill in fills:
        if str(fill.get("symbol")) != symbol or not fill.get("reduce_only"):
            continue
        ts = pd.Timestamp(str(fill.get("timestamp")))
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        if ts == exit_ts:
            meta = (fill.get("raw") or {}).get("intent_metadata") or {}
            return meta.get("exit_reason") or None
    return None


def compute_trade_forensics(
    bars: pd.DataFrame,
    *,
    side: str,
    entry_ts: pd.Timestamp,
    entry_price: float,
    exit_ts: pd.Timestamp,
    exit_price: float,
    exit_reason: str | None = None,
    post_bars: Sequence[int] = POST_BARS,
    stop_grid: Sequence[float] = STOP_GRID,
) -> dict[str, Any]:
    """Path metrics for one closed trade from a single-symbol bars frame.

    ``bars`` needs timestamp/high/low/close columns sorted ascending; the
    frame may be partial — coverage flags say what was computable.
    """
    direction = 1.0 if side == "long" else -1.0
    stamps = pd.to_datetime(bars["timestamp"], utc=True)
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)
    close = bars["close"].astype(float)

    hold_mask = (stamps > entry_ts) & (stamps <= exit_ts)
    post_mask = stamps > exit_ts
    hold_high, hold_low = high[hold_mask], low[hold_mask]
    post_close = close[post_mask].head(max(post_bars))
    post_high = high[post_mask].head(max(post_bars))
    post_low = low[post_mask].head(max(post_bars))

    realized_bps = _bps(direction * (exit_price - entry_price), entry_price)

    hold_covered = bool(hold_mask.any()) and bool(stamps.iloc[0] <= entry_ts)
    if hold_mask.any():
        favorable_extreme = hold_high.max() if side == "long" else hold_low.min()
        adverse_extreme = hold_low.min() if side == "long" else hold_high.max()
        mfe_bps = _bps(direction * (favorable_extreme - entry_price), entry_price)
        mae_bps = _bps(direction * (entry_price - adverse_extreme), entry_price)
    else:
        mfe_bps = mae_bps = None

    post_favorable: dict[str, float | None] = {}
    for k in post_bars:
        if len(post_close) >= k:
            post_favorable[f"+{k}"] = _bps(
                direction * (post_close.iloc[k - 1] - exit_price), entry_price
            )
        else:
            post_favorable[f"+{k}"] = None
    if len(post_close):
        best_extreme = post_high.max() if side == "long" else post_low.min()
        post_best_bps = _bps(direction * (best_extreme - exit_price), entry_price)
        # Did price come back through the entry after the exit? For a
        # stopped-out trade this is the "the stop was noise" signature.
        reentered = (
            bool((post_low <= entry_price).any())
            if side == "short"
            else bool((post_high >= entry_price).any())
        )
    else:
        post_best_bps = None
        reentered = None

    # Only bracket (stop) closes lack an exit_reason: strategy closes carry
    # intent metadata, the engine's bracket fills do not. Label them instead
    # of leaking None/"unknown" into the aggregate.
    reason = exit_reason or BRACKET_EXIT_REASON

    # Wider-stop counterfactual. For a stop-out the hypothetical wider-stop
    # hold CONTINUES past the actual exit, so the adverse scan must extend
    # through the post window too — scanning only the truncated hold would
    # overstate survival. Time/signal exits end the position regardless of
    # stop width, so their scan is exactly the hold.
    if reason == BRACKET_EXIT_REASON and len(post_close):
        adverse_scan_high = pd.concat([hold_high, post_high])
        adverse_scan_low = pd.concat([hold_low, post_low])
    else:
        adverse_scan_high, adverse_scan_low = hold_high, hold_low
    if len(adverse_scan_high):
        scan_adverse = (
            adverse_scan_low.min() if side == "long" else adverse_scan_high.max()
        )
        scan_mae_bps = _bps(direction * (entry_price - scan_adverse), entry_price)
    else:
        scan_mae_bps = None
    stop_survival = {
        f"{pct:g}": (scan_mae_bps is not None and scan_mae_bps < pct * 1e4)
        for pct in stop_grid
    }

    return {
        "side": side,
        "entry_ts": entry_ts.isoformat(),
        "exit_ts": exit_ts.isoformat(),
        "entry_price": entry_price,
        "exit_price": exit_price,
        "exit_reason": reason,
        "realized_bps": realized_bps,
        "hold_mfe_bps": mfe_bps,
        "hold_mae_bps": mae_bps,
        "post_exit_favorable_bps": post_favorable,
        "post_exit_best_bps": post_best_bps,
        "post_exit_through_entry": reentered,
        "stop_survives": stop_survival,
        "coverage": {
            "hold": hold_covered,
            "post_bars": int(len(post_close)),
            "post_bars_wanted": int(max(post_bars)),
        },
    }


def forensics_for_closed_trades(
    bars_by_symbol: Mapping[str, pd.DataFrame],
    closed_trades: Sequence[Mapping[str, Any]],
    fills: Sequence[Mapping[str, Any]],
    *,
    post_bars: Sequence[int] = POST_BARS,
    stop_grid: Sequence[float] = STOP_GRID,
) -> list[dict[str, Any]]:
    """Forensics for each closed trade whose entry fill can be matched.

    ``closed_trades`` rows are the recorded trade-close shape (symbol, the
    CLOSING fill's side, price/avg_price, timestamp/closed_at, optional raw
    intent metadata); ``fills`` supplies entry candidates.
    """
    rows: list[dict[str, Any]] = []
    for trade in closed_trades:
        symbol = str(trade.get("symbol"))
        bars = bars_by_symbol.get(symbol)
        if bars is None or bars.empty:
            continue
        exit_ts = pd.Timestamp(str(trade.get("closed_at") or trade.get("timestamp")))
        if exit_ts.tzinfo is None:
            exit_ts = exit_ts.tz_localize("UTC")
        entry = match_entry_fill(fills, symbol=symbol, exit_ts=exit_ts)
        if entry is None:
            continue
        entry_ts = pd.Timestamp(str(entry.get("timestamp")))
        if entry_ts.tzinfo is None:
            entry_ts = entry_ts.tz_localize("UTC")
        raw = trade.get("raw") or {}
        meta = raw.get("intent_metadata") or {}
        entry_raw = entry.get("raw") or {}
        entry_meta = entry_raw.get("intent_metadata") or {}
        exit_reason = meta.get("exit_reason") or trade.get("exit_reason")
        if not exit_reason:
            # Forward trade-close rows carry no intent metadata — the reason
            # lives on the CLOSING fill (same symbol/timestamp, reduce_only).
            exit_reason = _closing_fill_reason(fills, symbol=symbol, exit_ts=exit_ts)
        row = compute_trade_forensics(
            bars,
            side=position_side_of_close(str(trade.get("side"))),
            entry_ts=entry_ts,
            entry_price=float(entry.get("avg_price") or 0.0),
            exit_ts=exit_ts,
            exit_price=float(trade.get("price") or trade.get("avg_price") or 0.0),
            exit_reason=exit_reason,
            post_bars=post_bars,
            stop_grid=stop_grid,
        )
        row["symbol"] = symbol
        row["entry_reason"] = entry_meta.get("entry_reason")
        row["net_pnl"] = trade.get("net_pnl") or trade.get("realized_pnl_delta")
        rows.append(row)
    return rows


def aggregate_trade_forensics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Population-level exit-quality summary, grouped by exit reason.

    This is the statistically meaningful view — per-trade rows are anecdotes.
    """

    def _mean(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 1) if values else None

    by_reason: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        by_reason.setdefault(str(row.get("exit_reason") or "unknown"), []).append(row)

    groups: dict[str, Any] = {}
    for reason, members in sorted(by_reason.items()):
        realized = [float(m["realized_bps"]) for m in members]
        maes = [
            float(m["hold_mae_bps"])
            for m in members
            if m.get("hold_mae_bps") is not None
        ]
        mfes = [
            float(m["hold_mfe_bps"])
            for m in members
            if m.get("hold_mfe_bps") is not None
        ]
        post: dict[str, float | None] = {}
        for key in members[0].get("post_exit_favorable_bps") or {}:
            vals = [
                float(m["post_exit_favorable_bps"][key])
                for m in members
                if (m.get("post_exit_favorable_bps") or {}).get(key) is not None
            ]
            post[key] = _mean(vals)
        survival: dict[str, float] = {}
        for width in members[0].get("stop_survives") or {}:
            flags = [
                bool(m["stop_survives"][width])
                for m in members
                if m.get("stop_survives")
            ]
            survival[width] = round(sum(flags) / len(flags), 3) if flags else 0.0
        through = [m.get("post_exit_through_entry") for m in members]
        through_known = [t for t in through if t is not None]
        groups[reason] = {
            "count": len(members),
            "avg_realized_bps": _mean(realized),
            "total_realized_bps": round(sum(realized), 1),
            "avg_hold_mae_bps": _mean(maes),
            "avg_hold_mfe_bps": _mean(mfes),
            "avg_post_exit_favorable_bps": post,
            "stop_survival_rate": survival,
            "post_exit_through_entry_rate": (
                round(sum(bool(t) for t in through_known) / len(through_known), 3)
                if through_known
                else None
            ),
        }
    return {"trades": len(rows), "by_exit_reason": groups}
