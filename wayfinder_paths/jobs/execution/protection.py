"""Live reconciliation for venue-native stops and multi-leg risk groups."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.execution.engine import (
    EngineState,
    prune_closed_protection_groups,
)
from wayfinder_paths.jobs.execution.primitives import FillEvent, OrderIntent
from wayfinder_paths.jobs.execution.venues import VenueState


async def monitor_native_protection(
    *,
    mode: str,
    state: EngineState,
    brokers: Mapping[str, Any],
    venue_states: Mapping[str, VenueState],
    now: pd.Timestamp,
) -> tuple[
    list[dict[str, Any]],
    list[FillEvent],
    list[dict[str, Any]],
    str | None,
]:
    """Inspect only this job's stop cloids and enforce configured group loss.

    A missing owned stop, an orphan pair leg, or a combined-PnL breach closes
    the affected venue positions reduce-only and asks the caller to latch a
    durable halt. Other orders on a shared wallet are never modified.
    """
    protections = state.native_protections
    if mode != "live" or not venue_states:
        return [], [], [], None

    venue_positions: dict[str, Any] = {}
    open_cloids: set[str] = set()
    for venue_state in venue_states.values():
        venue_positions.update(venue_state.positions)
        open_cloids.update(
            str(order.get("cloid"))
            for order in venue_state.open_orders
            if order.get("cloid")
        )

    reasons: list[str] = []
    close_symbols: set[str] = set()
    groups = dict(state.strategy_state.get("protection_groups") or {})
    for protection in protections.values():
        raw_group = protection.get("protection_group") or {}
        group_id = str(raw_group.get("id") or "")
        if group_id:
            groups.setdefault(group_id, raw_group)
    breached_groups: set[str] = set()

    for symbol, protection in protections.items():
        cloid = str(protection.get("client_order_id") or "")
        position = venue_positions.get(symbol)
        protected_size = _optional_positive_float(protection.get("size"))
        size_matches = (
            position is None
            or protected_size is None
            or abs(position.size - protected_size) <= 1e-6 * max(1.0, position.size)
        )
        if cloid and cloid in open_cloids and size_matches:
            continue
        group = protection.get("protection_group") or {}
        group_id = str(group.get("id") or "")
        group_symbols = [str(value) for value in group.get("symbols") or []]
        affected = group_symbols or [symbol]
        close_symbols.update(
            affected_symbol
            for affected_symbol in affected
            if affected_symbol in venue_positions
        )
        if group_id:
            breached_groups.add(group_id)
        _start_cooldown(state, symbol, now)
        reasons.append(
            f"owned native stop missing for {symbol}"
            if size_matches
            else f"native stop size does not match the {symbol} position"
        )

    # A required bracket with no confirmed protection is durable evidence of
    # an interrupted install/unwind. Retry the targeted close on every monitor
    # tick instead of leaving the halted position silently unprotected.
    for symbol, bracket in state.brackets.items():
        if (
            symbol in protections
            or symbol not in venue_positions
            or not bracket.get("native_required")
        ):
            continue
        raw_group = bracket.get("protection_group") or {}
        group_id = str(raw_group.get("id") or "")
        group_symbols = [str(value) for value in raw_group.get("symbols") or []]
        close_symbols.update(group_symbols or [symbol])
        if group_id:
            breached_groups.add(group_id)
            groups.setdefault(group_id, raw_group)
        _start_cooldown(state, symbol, now)
        reasons.append(f"no confirmed native stop for {symbol}")

    for group_id, raw_group in groups.items():
        if not isinstance(raw_group, Mapping):
            continue
        symbols = [str(value) for value in raw_group.get("symbols") or []]
        held = [symbol for symbol in symbols if symbol in venue_positions]
        if not held:
            continue
        if len(held) != len(symbols):
            breached_groups.add(str(group_id))
            close_symbols.update(held)
            reasons.append(f"protection group {group_id} has an orphan leg")
            continue
        combined_pnl = _ledger_attributed_group_pnl(state, venue_positions, symbols)
        if combined_pnl is None:
            continue
        loss_limit = _group_loss_limit(raw_group)
        if loss_limit is not None and combined_pnl <= -loss_limit:
            breached_groups.add(str(group_id))
            close_symbols.update(symbols)
            reasons.append(
                f"protection group {group_id} ledger-attributed loss "
                f"{combined_pnl:.2f} breached {-loss_limit:.2f}"
            )

    if not reasons:
        return [], [], [], None

    notes: list[dict[str, Any]] = [
        {
            "kind": "native_protection_breach",
            "reason": reason,
            "timestamp": now.isoformat(),
        }
        for reason in reasons
    ]
    fills: list[FillEvent] = []
    trade_rows: list[dict[str, Any]] = []
    for symbol in sorted(close_symbols):
        position = venue_positions.get(symbol)
        if position is None:
            continue
        # Mirror the engine's own unwind bound (_record_fill_and_protect):
        # never close more than the JOB LEDGER attributes to this job. On a
        # shared wallet the venue position includes other owners' size; with
        # no ledger position at all there is nothing of ours to close —
        # journal the skip instead of touching the wallet.
        ledger_position = state.ledger.positions.get(symbol)
        close_size = (
            min(position.size, ledger_position.size)
            if ledger_position is not None
            else 0.0
        )
        if close_size <= 0:
            notes.append(
                {
                    "kind": "native_protection_close_skipped",
                    "symbol": symbol,
                    "reason": "no job-ledger position; venue position left untouched",
                    "venue_size": position.size,
                }
            )
            continue
        protection = protections.get(symbol) or {}
        bracket = state.brackets.get(symbol) or {}
        venue = str(protection.get("venue") or bracket.get("venue") or "")
        broker = brokers.get(venue) or (
            next(iter(brokers.values())) if len(brokers) == 1 else None
        )
        if broker is None:
            notes.append(
                {
                    "kind": "native_protection_close_failed",
                    "symbol": symbol,
                    "reason": "no broker for protected position",
                }
            )
            continue
        intent = OrderIntent(
            action="CLOSE",
            venue=venue or next(iter(brokers)),
            symbol=symbol,
            side="sell" if position.side == "long" else "buy",
            size=close_size,
            reduce_only=True,
            client_order_id=_monitor_cloid(symbol, now),
            metadata={"exit_reason": "native_protection_breach"},
        )
        fill = await broker.place(intent, timestamp=now.isoformat(), price=None)
        fills.append(fill)
        realized_before = state.ledger.realized_pnl
        state.ledger.apply_fill(fill)
        if fill.successful:
            prune_closed_protection_groups(state)
            row = fill.to_dict()
            row["realized_pnl_delta"] = state.ledger.realized_pnl - realized_before
            trade_rows.append(row)
            state.brackets.pop(symbol, None)
            await _cancel_closed_stop(
                broker=broker,
                symbol=symbol,
                protection=protection,
                state=state,
                notes=notes,
            )
        notes.append(
            {
                "kind": "native_protection_group_close",
                "symbol": symbol,
                "status": fill.status,
                "breached_groups": sorted(breached_groups),
            }
        )
    return notes, fills, trade_rows, "; ".join(dict.fromkeys(reasons))


def _start_cooldown(state: EngineState, symbol: str, now: pd.Timestamp) -> None:
    bracket = state.brackets.get(symbol) or {}
    cooldown_seconds = int(bracket.get("cooldown_seconds") or 0)
    if cooldown_seconds:
        cooldowns = state.strategy_state.setdefault("protection_cooldowns", {})
        cooldowns[symbol] = (now + pd.Timedelta(seconds=cooldown_seconds)).isoformat()


def _ledger_attributed_group_pnl(
    state: EngineState,
    venue_positions: Mapping[str, Any],
    symbols: list[str],
) -> float | None:
    """Group PnL scoped to this JOB's ledger exposure, not the whole wallet.

    Venue unrealized PnL covers every position on a (possibly shared) wallet;
    the group loss budget must only count the job's own risk. Each leg
    contributes ledger size (bounded by venue size) x the mark move off the
    LEDGER entry price — i.e. ledger notional x mark move — with the mark
    derived from venue telemetry (position_value / size, falling back to
    inverting the venue's unrealized PnL). Legs the ledger does not hold
    contribute nothing. Returns None when a mark cannot be derived for a held
    leg: the check skips that tick, same as the old missing-PnL skip."""
    total = 0.0
    for symbol in symbols:
        ledger_position = state.ledger.positions.get(symbol)
        if ledger_position is None or ledger_position.size <= 0:
            continue
        venue_position = venue_positions[symbol]
        mark = _venue_mark_price(venue_position)
        if mark is None:
            return None
        direction = 1.0 if ledger_position.side == "long" else -1.0
        size = min(ledger_position.size, venue_position.size)
        total += direction * (mark - ledger_position.avg_price) * size
    return total


def _venue_mark_price(position: Any) -> float | None:
    size = float(position.size or 0.0)
    if size <= 0:
        return None
    value = _optional_positive_float(position.metadata.get("position_value"))
    if value is not None:
        return value / size
    try:
        unrealized = float(position.metadata.get("unrealized_pnl"))
    except (TypeError, ValueError):
        return None
    direction = 1.0 if position.side == "long" else -1.0
    return float(position.avg_price) + direction * unrealized / size


def _group_loss_limit(group: Mapping[str, Any]) -> float | None:
    entry_equity = _optional_positive_float(group.get("entry_account_equity"))
    entry_gross = _optional_positive_float(group.get("entry_gross_notional"))
    limits = []
    if entry_equity is not None:
        limits.append(
            entry_equity * float(group.get("max_entry_equity_loss_pct") or 0.0)
        )
    if entry_gross is not None:
        limits.append(entry_gross * float(group.get("max_entry_gross_loss_pct") or 0.0))
    return min((value for value in limits if value > 0), default=None)


async def _cancel_closed_stop(
    *,
    broker: Any,
    symbol: str,
    protection: Mapping[str, Any],
    state: EngineState,
    notes: list[dict[str, Any]],
) -> None:
    cancel_stop = getattr(broker, "cancel_stop_loss", None)
    client_order_id = str(protection.get("client_order_id") or "")
    if cancel_stop is None or not client_order_id:
        return
    cancel_result = await cancel_stop(
        symbol=symbol,
        client_order_id=client_order_id,
    )
    notes.append(
        {"kind": "native_protection_group_stop_canceled", **cancel_result.to_dict()}
    )
    if cancel_result.confirmed:
        state.native_protections.pop(symbol, None)


def _monitor_cloid(symbol: str, now: pd.Timestamp) -> str:
    seed = f"protection|{symbol}|{now.isoformat()}"
    return f"0x{hashlib.sha256(seed.encode()).hexdigest()[:32]}"


def _optional_positive_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None
