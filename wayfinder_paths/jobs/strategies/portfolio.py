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
from datetime import UTC, datetime
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
    brackets: Mapping[str, Mapping[str, Any]] | None = None,
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
        frame = ctx.view.symbol_frame(symbol)
        # avg_price fallback when the view has no bars — same as the equity mark
        close = (
            float(frame["close"].iloc[-1]) if len(frame) else float(position.avg_price)
        )
        closes[symbol] = close
        direction = 1 if position.side == "long" else -1
        current[symbol] = direction * position.size * close / equity

    intents: list[dict[str, Any]] = []
    for symbol in sorted(set(targets) | set(current)):
        target = targets[symbol] if symbol in targets else 0.0
        held = current[symbol] if symbol in current else 0.0
        delta = target - held
        if abs(delta) < rebalance_threshold:
            continue
        if abs(delta) * equity < min_trade_notional:
            continue

        held_position = ctx.ledger.positions.get(symbol)
        flips = held and target and (held > 0) != (target > 0)
        if held_position is not None and (target == 0 or flips):
            intents.append(
                _close(symbol, held_position, venue, size=held_position.size)
            )
            held = 0.0
        elif held_position is not None and abs(target) < abs(held):
            # Same-sign shrink: close (|held| - |target|) worth of units.
            # symbol is in closes whenever a position exists (built above).
            size = (abs(held) - abs(target)) * equity / closes[symbol]
            intents.append(_close(symbol, position, venue, size=size))
            continue

        grow = target - held
        if target and abs(grow) > 0:
            if _cooldown_active(ctx, symbol):
                continue
            intent: dict[str, Any] = {
                "action": "OPEN",
                "venue": venue,
                "symbol": symbol,
                "side": "buy" if target > 0 else "sell",
                "notional": abs(grow) * equity,
                "metadata": {"target_weight": target},
            }
            if brackets and symbol in brackets:
                intent["bracket"] = dict(brackets[symbol])
            intents.append(intent)
    return intents


def _cooldown_active(ctx: ExecutionContext, symbol: str) -> bool:
    cooldowns = ctx.strategy_state.get("protection_cooldowns") or {}
    raw_expiry = cooldowns.get(symbol) if isinstance(cooldowns, Mapping) else None
    if not raw_expiry:
        return False
    try:
        expiry = datetime.fromisoformat(str(raw_expiry).replace("Z", "+00:00"))
        now = datetime.fromisoformat(str(ctx.timestamp).replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
    except ValueError:
        return True
    return now < expiry


def _close(symbol: str, position: Any, venue: str, *, size: float) -> dict[str, Any]:
    return {
        "action": "CLOSE",
        "venue": venue,
        "symbol": symbol,
        "side": "sell" if position.side == "long" else "buy",
        "size": size,
        "reduce_only": True,
        "metadata": {"position_side": position.side},
    }
