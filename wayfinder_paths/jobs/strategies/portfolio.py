"""Bridge from the legacy weights paradigm to jobs_v1 order intents.

The legacy vectorized engine (core/backtesting/backtester.py) consumes a
target-weight matrix; jobs_v1 strategies emit discrete OrderIntents. This
helper lets a weights-style strategy run under the jobs contract: compute
target weights inside decide(), call target_weights_to_intents(ctx, weights),
return the result. Pure — it reads only ctx — so it is purity-sandbox safe
and byte-deterministic for identical inputs.

Leverage is the caller's concern: either scale the weights (gross > 1 with
normalize_gross=False) or pass sizing_equity = equity * leverage.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from wayfinder_paths.jobs.execution.primitives import (
    ExecutionContext,
    mark_to_market_equity,
)


def target_weights_to_intents(
    ctx: ExecutionContext,
    weights: Mapping[str, float],
    *,
    venue: str = "hyperliquid",
    rebalance_threshold: float = 0.0,
    sizing_equity: float | None = None,
    normalize_gross: bool = True,
    min_trade_notional: float = 0.0,
) -> list[dict[str, Any]]:
    """Diff target weights against the current ledger and emit intents.

    Per symbol (positive weight = long, negative = short):
    - target 0 with an open position -> full reduce-only CLOSE
    - sign flip -> full CLOSE, then OPEN the opposite side
    - same-sign shrink -> partial reduce-only CLOSE
    - grow / new -> OPEN with notional = |delta| * equity
    - |delta| < rebalance_threshold or notional < min_trade_notional -> skip

    Gross normalization (legacy convention, backtester.py): when the summed
    |weights| exceed 1 and normalize_gross is True, weights are divided by
    gross so the portfolio never implicitly levers. Pass False when leverage
    via weights is intentional.
    """
    equity = float(
        sizing_equity if sizing_equity is not None else mark_to_market_equity(ctx)
    )
    if equity <= 0:
        return []

    targets = {str(symbol): float(weight) for symbol, weight in weights.items()}
    gross = sum(abs(weight) for weight in targets.values())
    if normalize_gross and gross > 1.0:
        targets = {symbol: weight / gross for symbol, weight in targets.items()}

    current: dict[str, float] = {}
    closes: dict[str, float] = {}
    for symbol, position in ctx.ledger.positions.items():
        try:
            close = float(ctx.view.latest(symbol)["close"])
        except ValueError:
            close = float(position.avg_price)  # same fallback as equity mark
        closes[symbol] = close
        direction = 1 if position.side == "long" else -1
        current[symbol] = direction * position.size * close / equity

    intents: list[dict[str, Any]] = []
    for symbol in sorted(set(targets) | set(current)):
        target = targets.get(symbol, 0.0)
        held = current.get(symbol, 0.0)
        delta = target - held
        if abs(delta) < rebalance_threshold:
            continue
        if abs(delta) * equity < min_trade_notional:
            continue

        position = ctx.ledger.positions.get(symbol)
        flips = held and target and (held > 0) != (target > 0)
        if position is not None and (target == 0 or flips):
            intents.append(_close(symbol, position, venue, size=position.size))
            held = 0.0
        elif position is not None and abs(target) < abs(held):
            # Same-sign shrink: close (|held| - |target|) worth of units.
            close = closes.get(symbol) or float(position.avg_price)
            size = (abs(held) - abs(target)) * equity / close
            intents.append(_close(symbol, position, venue, size=size))
            continue

        grow = target - held
        if target and abs(grow) > 0:
            intents.append(
                {
                    "action": "OPEN",
                    "venue": venue,
                    "symbol": symbol,
                    "side": "buy" if target > 0 else "sell",
                    "notional": abs(grow) * equity,
                    "metadata": {"target_weight": target},
                }
            )
    return intents


def _close(
    symbol: str, position: Any, venue: str, *, size: float
) -> dict[str, Any]:
    return {
        "action": "CLOSE",
        "venue": venue,
        "symbol": symbol,
        "side": "sell" if position.side == "long" else "buy",
        "size": size,
        "reduce_only": True,
        "metadata": {"position_side": position.side},
    }
