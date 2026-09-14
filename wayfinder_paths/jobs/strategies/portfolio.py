"""Bridge from the legacy weights paradigm to jobs_v1 order intents.

The legacy vectorized engine (core/backtesting/backtester.py) consumes a
target-weight matrix; jobs_v1 strategies emit discrete OrderIntents. This
helper lets a weights-style strategy run under the jobs contract: compute
target weights inside decide(), call target_weights_to_intents(ctx, weights),
return the result. Pure — it reads only ctx — so it is purity-sandbox safe
and byte-deterministic for identical inputs.

When ``ctx.params.leverage`` is set, the helper scales sizing equity so the
requested leverage changes the target exposure rather than multiplying each
rebalance delta. Opens are stamped so the engine-level fallback does not apply
the same leverage a second time.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from wayfinder_paths.jobs.execution.primitives import (
    ExecutionContext,
    mark_to_market_equity,
)
from wayfinder_paths.jobs.strategies._starter_utils import (
    protection_cooldown_active,
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
    leverage = 1.0
    try:
        candidate = float(ctx.params.get("leverage") or 1.0)
        if math.isfinite(candidate) and candidate > 0:
            leverage = candidate
    except (TypeError, ValueError):
        pass
    equity = float(
        sizing_equity
        if sizing_equity is not None
        else mark_to_market_equity(ctx) * leverage
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
            intents.append(_close(symbol, held_position, venue, size=size))
            continue

        grow = target - held
        if target and abs(grow) > 0:
            if protection_cooldown_active(ctx, symbol):
                continue
            metadata: dict[str, Any] = {"target_weight": target}
            if sizing_equity is not None:
                # An explicit sizing-equity override already owns the target
                # exposure; protect it from the engine's generic intent scaler.
                metadata["leverage_applied"] = True
            elif leverage != 1.0:
                metadata.update({"leverage_applied": True, "engine_leverage": leverage})
            intent: dict[str, Any] = {
                "action": "OPEN",
                "venue": venue,
                "symbol": symbol,
                "side": "buy" if target > 0 else "sell",
                "notional": abs(grow) * equity,
                "metadata": metadata,
            }
            if brackets and symbol in brackets:
                intent["bracket"] = dict(brackets[symbol])
            intents.append(intent)
    return intents


def _close(symbol: str, position: Any, venue: str, *, size: float) -> dict[str, Any]:
    return {
        "action": "CLOSE",
        "venue": venue,
        "symbol": symbol,
        "side": "sell" if position.side == "long" else "buy",
        "size": size,
        "reduce_only": True,
        "metadata": {"exit_reason": "target_weight", "position_side": position.side},
    }
