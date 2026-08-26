from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.execution.gates import latest_gate_state
from wayfinder_paths.jobs.execution.primitives import (
    DEFAULT_INITIAL_CAPITAL,
    REDUCE_ONLY_ACTIONS,
    BracketEngine,
    CompletedBarsView,
    ExecutionContext,
    ExecutionSpec,
    ExecutionTrace,
    FillEvent,
    OrderIntent,
    PositionLedger,
    RestingOrder,
    StateSnapshot,
    TradeCapacity,
    _float_or_none,
    bar_interval_seconds,
)
from wayfinder_paths.jobs.execution.purity import purity_sandbox
from wayfinder_paths.jobs.execution.venues import (
    Broker,
    MarketEvent,
    NativeProtectionBroker,
)

OPEN_SIDES_SHORT = frozenset({"short", "sell"})

# Ported from core/backtesting/constants.py (DEFAULT_MAINTENANCE_MARGINS);
# jobs symbols are bare, so keys are matched exact-first, then by the base
# symbol before "/" for legacy-style "HYPE/USDC:USDC" keys.
DEFAULT_MAINTENANCE_MARGIN_BY_SYMBOL: dict[str, float] = {
    "HYPE": 1 / 20.0,
    "ASTER": 1 / 16.0,
    "DYDX": 1 / 20.0,
    "GMX": 1 / 20.0,
    "APEX": 1 / 20.0,
    "AVNT": 1 / 10.0,
    "BTC": 1 / 100.0,
}


@dataclass(frozen=True)
class LiquidationConfig:
    """Backtest-only liquidation model (legacy total-wipe port from
    core/backtesting/backtester.py). Constructed exclusively by
    simulate_execution from params["enable_liquidation"]; the live/paper
    driver never passes it — the venue does real liquidations there."""

    maintenance_margin_rate: float = 0.05
    maintenance_margin_by_symbol: Mapping[str, float] = field(default_factory=dict)
    liquidation_buffer: float = 0.001
    initial_capital: float = DEFAULT_INITIAL_CAPITAL

    @classmethod
    def from_params(cls, params: Mapping[str, Any]) -> LiquidationConfig | None:
        if not params.get("enable_liquidation"):
            return None
        by_symbol = dict(DEFAULT_MAINTENANCE_MARGIN_BY_SYMBOL)
        overrides = params.get("maintenance_margin_by_symbol") or {}
        by_symbol.update({str(key): float(value) for key, value in overrides.items()})
        # Explicit None checks: 0.0 is a legitimate rate/buffer, not "unset".
        raw_rate = params.get("maintenance_margin_rate")
        raw_buffer = params.get("liquidation_buffer")
        return cls(
            maintenance_margin_rate=0.05 if raw_rate is None else float(raw_rate),
            maintenance_margin_by_symbol=by_symbol,
            liquidation_buffer=0.001 if raw_buffer is None else float(raw_buffer),
            initial_capital=float(
                params.get("initial_capital") or DEFAULT_INITIAL_CAPITAL
            ),
        )

    def rate_for(self, symbol: str) -> float:
        if symbol in self.maintenance_margin_by_symbol:
            return self.maintenance_margin_by_symbol[symbol]
        base = symbol.split("/")[0]
        if base in self.maintenance_margin_by_symbol:
            return self.maintenance_margin_by_symbol[base]
        return self.maintenance_margin_rate


@dataclass
class EngineState:
    """Durable engine state. In-memory for backtest; persisted to
    state/engine_state.json between live/paper ticks (the runner spawns a fresh
    subprocess per tick, so nothing survives in memory)."""

    ledger: PositionLedger = field(default_factory=PositionLedger)
    brackets: dict[str, dict[str, Any]] = field(default_factory=dict)
    native_protections: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_intents: list[OrderIntent] = field(default_factory=list)
    resting_orders: dict[str, RestingOrder] = field(default_factory=dict)
    last_processed_bar_ts: str | None = None
    daily_notional: dict[str, float] = field(default_factory=dict)
    revision: str | None = None
    strategy_state: dict[str, Any] = field(default_factory=dict)
    liquidated_at: str | None = None
    # Mode that produced this state ("paper" | "live"); the driver archives
    # and resets state on a mode flip so paper test ticks can't pollute live.
    mode: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ledger": self.ledger.snapshot(),
            "brackets": dict(self.brackets),
            "native_protections": dict(self.native_protections),
            "pending_intents": [intent.to_dict() for intent in self.pending_intents],
            "resting_orders": {
                client_order_id: order.to_dict()
                for client_order_id, order in self.resting_orders.items()
            },
            "last_processed_bar_ts": self.last_processed_bar_ts,
            "daily_notional": dict(self.daily_notional),
            "revision": self.revision,
            "strategy_state": dict(self.strategy_state),
            "liquidated_at": self.liquidated_at,
            "mode": self.mode,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> EngineState:
        payload = data or {}
        return cls(
            ledger=PositionLedger.restore(payload.get("ledger")),
            brackets=dict(payload.get("brackets") or {}),
            native_protections=dict(payload.get("native_protections") or {}),
            pending_intents=[
                OrderIntent.from_any(item)
                for item in payload.get("pending_intents") or []
            ],
            resting_orders={
                str(client_order_id): RestingOrder.from_dict(order)
                for client_order_id, order in (
                    payload.get("resting_orders") or {}
                ).items()
            },
            last_processed_bar_ts=payload.get("last_processed_bar_ts"),
            daily_notional={
                str(key): float(value)
                for key, value in (payload.get("daily_notional") or {}).items()
            },
            revision=payload.get("revision"),
            strategy_state=dict(payload.get("strategy_state") or {}),
            liquidated_at=payload.get("liquidated_at"),
            mode=payload.get("mode"),
        )

    @classmethod
    def load(cls, path: str | Path) -> EngineState:
        location = Path(path)
        if not location.exists():
            return cls()
        return cls.from_dict(json.loads(location.read_text(encoding="utf-8")))

    def save(self, path: str | Path) -> None:
        location = Path(path)
        from wayfinder_paths.runner.monitor_state import atomic_write_json

        atomic_write_json(location, self.to_dict(), default=str)


@dataclass
class TickResult:
    skipped: bool = False
    skip_reason: str | None = None
    bar_timestamp: str | None = None
    intents: list[OrderIntent] = field(default_factory=list)
    fills: list[FillEvent] = field(default_factory=list)
    trade_rows: list[dict[str, Any]] = field(default_factory=list)
    guard_events: list[dict[str, Any]] = field(default_factory=list)
    ledger_snapshot: dict[str, Any] = field(default_factory=dict)
    gates: dict[str, dict[str, Any]] = field(default_factory=dict)
    snapshot: StateSnapshot = field(default_factory=StateSnapshot)


async def run_tick(
    strategy: Any,
    *,
    view: CompletedBarsView,
    brokers: Mapping[str, Broker],
    state: EngineState,
    spec: ExecutionSpec,
    params: dict[str, Any],
    timestamp: pd.Timestamp,
    snapshot: StateSnapshot | None = None,
    capacity: TradeCapacity | None = None,
    events: list[MarketEvent] | None = None,
    auto_limits: Mapping[str, Any] | None = None,
    trace: ExecutionTrace | None = None,
    enforce_purity: bool = True,
    client_order_prefix: str | None = None,
    liquidation: LiquidationConfig | None = None,
) -> TickResult:
    """One engine tick, identical across backtest, paper, and live.

    Step order mirrors the original simulator loop so backtest results carry
    over unchanged: bar tick -> settle pending intents at this bar's open ->
    market events -> emulated brackets -> decide() -> validate + route intents.

    `liquidation` is a backtest-only kwarg: simulate_execution constructs it
    from params; the live/paper driver must never pass it (real venues handle
    their own liquidations).
    """
    snapshot = snapshot or StateSnapshot(status="valid")
    trace = trace or ExecutionTrace(execution_spec=spec.to_dict())
    result = TickResult(snapshot=snapshot)
    try:
        return await _run_tick_inner(
            strategy,
            view=view,
            brokers=brokers,
            state=state,
            spec=spec,
            params=params,
            timestamp=timestamp,
            snapshot=snapshot,
            capacity=capacity,
            events=events,
            auto_limits=auto_limits,
            trace=trace,
            enforce_purity=enforce_purity,
            client_order_prefix=client_order_prefix,
            liquidation=liquidation,
            result=result,
        )
    finally:
        trace.guard_events.extend(result.guard_events)


async def _run_tick_inner(
    strategy: Any,
    *,
    view: CompletedBarsView,
    brokers: Mapping[str, Broker],
    state: EngineState,
    spec: ExecutionSpec,
    params: dict[str, Any],
    timestamp: pd.Timestamp,
    snapshot: StateSnapshot,
    capacity: TradeCapacity | None,
    events: list[MarketEvent] | None,
    auto_limits: Mapping[str, Any] | None,
    trace: ExecutionTrace,
    enforce_purity: bool,
    client_order_prefix: str | None,
    liquidation: LiquidationConfig | None,
    result: TickResult,
) -> TickResult:
    bar_ts = view.timestamps[-1] if view.timestamps else None
    if bar_ts is None:
        result.skipped = True
        result.skip_reason = "no_bars"
        return result
    bar_iso = bar_ts.isoformat()
    result.bar_timestamp = bar_iso

    if state.last_processed_bar_ts == bar_iso:
        result.skipped = True
        result.skip_reason = "no_new_bar"
        return result

    if liquidation is not None and state.liquidated_at:
        # Legacy `break` equivalent: once liquidated, the run is over.
        state.last_processed_bar_ts = bar_iso
        result.skipped = True
        result.skip_reason = "liquidated"
        return result

    stale = _is_stale(bar_ts, timestamp, spec)
    if stale:
        snapshot.status = "stale"
        snapshot.reason = stale
        result.guard_events.append(
            {"kind": "stale_data", "reason": stale, "timestamp": bar_iso}
        )
        policy = str(spec.data_contract.get("stale_policy") or "skip")
        if policy == "skip":
            result.skipped = True
            result.skip_reason = "stale_data"
            return result
        if policy == "flat":
            await flatten_positions(
                brokers=brokers,
                state=state,
                view=view,
                timestamp=bar_iso,
                trace=trace,
                result=result,
            )
            state.last_processed_bar_ts = bar_iso
            result.ledger_snapshot = state.ledger.snapshot()
            return result
        # "decide_anyway" falls through with snapshot.status == "stale"

    bars_by_symbol = _bars_at_timestamp(view, bar_ts)
    if not bars_by_symbol:
        result.skipped = True
        result.skip_reason = "no_bars_at_timestamp"
        return result
    result.gates = latest_gate_state(view)
    state.ledger.on_bar_tick(bar_ts)

    await _settle_resting_orders(
        brokers=brokers,
        state=state,
        bars_by_symbol=bars_by_symbol,
        params=params,
        timestamp=bar_iso,
        trace=trace,
        result=result,
        reduce_only=False,
    )

    # A symbol absent from this timestamp's bars has no market to fill against.
    # Never fall back to another symbol's bar (previously `default_bar`, the first
    # bar in the dict) — that fills e.g. an MU order at GOLD's price. Next-bar-open
    # intents are deferred until the symbol prints; immediate intents (below) are
    # dropped and the strategy re-emits when it can be priced honestly.
    deferred_intents: list[OrderIntent] = []
    for intent in state.pending_intents:
        bar = bars_by_symbol.get(intent.symbol)
        if bar is None:
            deferred_intents.append(intent)
            continue
        fill = await _place(
            brokers, intent, price=bar.open, timestamp=bar_iso, result=result
        )
        if fill is not None:
            await _record_fill_and_protect(
                fill,
                intent=intent,
                brokers=brokers,
                state=state,
                trace=trace,
                result=result,
                timestamp=bar_iso,
            )
    state.pending_intents = deferred_intents

    reference_prices = (
        {symbol: float(view.latest(symbol)["close"]) for symbol in view.symbols}
        if events
        else {}
    )
    for event in events or []:
        _apply_market_event(
            event,
            state=state,
            trace=trace,
            result=result,
            timestamp=bar_iso,
            reference_prices=reference_prices,
        )

    await _evaluate_brackets(
        brokers=brokers,
        state=state,
        bars_by_symbol={
            symbol: bar.to_dict() for symbol, bar in bars_by_symbol.items()
        },
        timestamp=bar_iso,
        trace=trace,
        result=result,
    )

    # Conservative same-bar ordering: an existing stop wins over a resting
    # take-profit when one OHLC candle crosses both. Entry limits settle before
    # brackets so a newly filled position is still stopped on its fill bar.
    await _settle_resting_orders(
        brokers=brokers,
        state=state,
        bars_by_symbol=bars_by_symbol,
        params=params,
        timestamp=bar_iso,
        trace=trace,
        result=result,
        reduce_only=True,
    )

    if liquidation is not None and state.ledger.positions:
        # After settlement + funding + brackets, before decide(): legacy
        # ordering, and a breach means decide() never runs on this bar.
        breached = await _check_liquidation(
            brokers=brokers,
            state=state,
            bars_by_symbol=bars_by_symbol,
            config=liquidation,
            timestamp=bar_iso,
            trace=trace,
            result=result,
        )
        if breached:
            state.last_processed_bar_ts = bar_iso
            ledger_snapshot = state.ledger.snapshot()
            result.ledger_snapshot = ledger_snapshot
            trace.ledger_snapshots.append({"timestamp": bar_iso, **ledger_snapshot})
            trace.runs.append(
                {
                    "timestamp": bar_iso,
                    "visible_bar_count": len(view),
                    "visible_latest_timestamp": _latest_visible_timestamp(view),
                    "guard_event_count": len(result.guard_events),
                    "gates": result.gates,
                }
            )
            return result

    ctx = ExecutionContext(
        view=view,
        ledger=state.ledger,
        state_snapshot=snapshot,
        capacity=capacity,
        params=params,
        timestamp=bar_iso,
        execution_spec=spec,
        # Same mutable dict as EngineState: decide() mutations persist across
        # ticks and are captured in engine_state_pre for exact replay.
        strategy_state=state.strategy_state,
        # Strategies may inspect but must not mutate durable route state.
        resting_orders=tuple(
            RestingOrder.from_dict(order.to_dict())
            for order in state.resting_orders.values()
        ),
    )
    decide = getattr(strategy, "decide", strategy)
    network_violations: list[str] = []
    sandbox = (
        purity_sandbox(
            network_policy=str(spec.validation.get("purity") or "warn"),
            violations=network_violations,
        )
        if enforce_purity
        else contextlib.nullcontext()
    )
    with sandbox:
        decided = decide(ctx)
        if asyncio.iscoroutine(decided):
            decided = await decided
    for violation in network_violations:
        result.guard_events.append(
            {"kind": "purity_warning", "reason": violation, "timestamp": bar_iso}
        )
    match decided:
        case None:
            decided = []
        case Mapping():
            decided = [decided]
        case _:
            decided = list(decided)
    intents = [OrderIntent.from_any(item) for item in decided]
    _apply_engine_leverage(intents, params)

    for index, intent in enumerate(intents):
        if (client_order_prefix or intent.limit_price is not None) and (
            intent.client_order_id is None
        ):
            # Deterministic per (job, bar, slot): an order submitted just before
            # a SIGKILL is recognized as ours on the next tick's fetch_state.
            seed = f"{client_order_prefix or 'jobs-v1'}|{bar_iso}|{index}"
            digest = hashlib.sha256(seed.encode()).hexdigest()
            intent.client_order_id = f"0x{digest[:32]}"
        trace.intents.append({"timestamp": bar_iso, **intent.to_dict()})
        if snapshot.status != "valid" and not intent.reduce_only:
            # Reduce-only mode: never add risk against stale/ambiguous state.
            result.guard_events.append(
                {
                    "kind": "intent_rejected",
                    "reason": (
                        f"snapshot is {snapshot.status}; only reduce-only intents "
                        "are routed"
                    ),
                    "intent": intent.to_dict(),
                    "timestamp": bar_iso,
                }
            )
            continue
        bar = bars_by_symbol.get(intent.symbol)
        # Price/validate against the symbol's OWN last-known close when it has no
        # bar this timestamp — never another symbol's bar (the old `default_bar`
        # fallback filled e.g. an MU order at GOLD's price). The fill itself is
        # dropped below when there's no current bar.
        if bar is not None:
            ref_price = bar.close
        elif intent.symbol in view.symbols:
            ref_price = float(view.latest(intent.symbol)["close"])
        else:
            ref_price = 0.0
        rejection = _validate_intent(
            intent,
            brokers=brokers,
            auto_limits=auto_limits,
            state=state,
            ref_price=ref_price,
            bar_iso=bar_iso,
        )
        if rejection:
            result.guard_events.append(
                {
                    "kind": "intent_rejected",
                    "reason": rejection,
                    "intent": intent.to_dict(),
                    "timestamp": bar_iso,
                }
            )
            continue
        if bar is None:
            # Passed validation but there's no market for this symbol this bar —
            # do not fill against another symbol's price. Drop it; the strategy
            # re-emits next bar when the symbol prints and can be priced honestly.
            result.guard_events.append(
                {
                    "kind": "intent_rejected",
                    "reason": f"no bar for {intent.symbol} at this timestamp",
                    "intent": intent.to_dict(),
                    "timestamp": bar_iso,
                }
            )
            continue
        result.intents.append(intent)
        if intent.reduce_only and intent.limit_price is None:
            _drop_resting_orders(state, symbol=intent.symbol, reduce_only=True)
        if not intent.reduce_only:
            notional = _intent_notional(intent, ref_price)
            if notional is not None:
                day = bar_iso[:10]
                state.daily_notional[day] = (
                    state.daily_notional.get(day, 0.0) + notional
                )
        if intent.limit_price is not None:
            fill = await _place(
                brokers, intent, price=None, timestamp=bar_iso, result=result
            )
            if fill is not None and fill.status == "resting":
                if intent.client_order_id is None:  # pragma: no cover - guarded above
                    raise RuntimeError("resting limit order missing client order id")
                state.resting_orders[intent.client_order_id] = RestingOrder(
                    intent=intent,
                    submitted_at=bar_iso,
                    order_id=fill.order_id,
                )
                result.guard_events.append(
                    {
                        "kind": "limit_resting",
                        "client_order_id": intent.client_order_id,
                        "order_id": fill.order_id,
                        "symbol": intent.symbol,
                        "timestamp": bar_iso,
                    }
                )
            elif fill is not None:
                await _record_fill_and_protect(
                    fill,
                    intent=intent,
                    brokers=brokers,
                    state=state,
                    trace=trace,
                    result=result,
                    timestamp=bar_iso,
                )
        elif spec.fill_model == "next_bar_open":
            state.pending_intents.append(intent)
        else:
            price = (
                (
                    _float_or_none(intent.metadata.get("replay_price"))
                    or intent.limit_price
                )
                if spec.fill_model == "replay"
                else None
            )
            if price is None:
                price = ref_price
            fill = await _place(
                brokers, intent, price=price, timestamp=bar_iso, result=result
            )
            if fill is not None:
                await _record_fill_and_protect(
                    fill,
                    intent=intent,
                    brokers=brokers,
                    state=state,
                    trace=trace,
                    result=result,
                    timestamp=bar_iso,
                )

    state.last_processed_bar_ts = bar_iso
    ledger_snapshot = state.ledger.snapshot()
    result.ledger_snapshot = ledger_snapshot
    trace.ledger_snapshots.append({"timestamp": bar_iso, **ledger_snapshot})
    trace.runs.append(
        {
            "timestamp": bar_iso,
            # len(view) == row count; avoids a full DataFrame copy per bar.
            "visible_bar_count": len(ctx.view),
            # A bounded multi-symbol window can legitimately lose more sparse
            # rows than it gains at the next timestamp. The latest visible
            # timestamp is the invariant that actually proves causal replay.
            "visible_latest_timestamp": _latest_visible_timestamp(ctx.view),
            "guard_event_count": len(result.guard_events),
            "gates": result.gates,
        }
    )
    return result


async def _settle_resting_orders(
    *,
    brokers: Mapping[str, Broker],
    state: EngineState,
    bars_by_symbol: Mapping[str, Any],
    params: Mapping[str, Any],
    timestamp: str,
    trace: ExecutionTrace,
    result: TickResult,
    reduce_only: bool,
) -> None:
    """Resolve paper/backtest limit orders against the next completed OHLC bar.

    A touch is not enough: the bar must trade through the limit by the declared
    buffer.  That is deliberately conservative about queue position when the
    historical dataset has candles rather than order-level queue events.  Live
    orders are reconciled from venue order/fill state by the driver and must
    never be synthesized from candles.
    """
    if state.mode == "live" or not state.resting_orders:
        return
    trade_through_bps = max(float(params.get("maker_trade_through_bps") or 1.0), 0.0)
    buffer = trade_through_bps / 10_000.0
    for client_order_id, order in list(state.resting_orders.items()):
        if client_order_id not in state.resting_orders:
            continue
        intent = order.intent
        if intent.reduce_only != reduce_only:
            continue
        bar = bars_by_symbol.get(intent.symbol)
        if bar is None:
            continue
        order.age_bars += 1
        limit_price = float(intent.limit_price or 0.0)
        wants_buy = str(intent.side).lower() in {"buy", "long"}
        traded_through = (
            float(bar.low) <= limit_price * (1.0 - buffer)
            if wants_buy
            else float(bar.high) >= limit_price * (1.0 + buffer)
        )
        if traded_through:
            state.resting_orders.pop(client_order_id, None)
            fill_intent = replace(
                intent,
                metadata={**intent.metadata, "_resting_fill": True},
            )
            fill = await _place(
                brokers,
                fill_intent,
                price=limit_price,
                timestamp=timestamp,
                result=result,
            )
            if fill is not None:
                await _record_fill_and_protect(
                    fill,
                    intent=intent,
                    brokers=brokers,
                    state=state,
                    trace=trace,
                    result=result,
                    timestamp=timestamp,
                )
                if (
                    reduce_only
                    and fill.successful
                    and intent.metadata.get("move_stop_to_break_even")
                    and intent.symbol in state.ledger.positions
                ):
                    # A stage-one fill can tighten the stop. Re-evaluate that
                    # new stop on the same OHLC candle before a later target;
                    # conservative ordering assumes the adverse path when the
                    # candle contains both prices.
                    await _evaluate_brackets(
                        brokers=brokers,
                        state=state,
                        bars_by_symbol={intent.symbol: bar.to_dict()},
                        timestamp=timestamp,
                        trace=trace,
                        result=result,
                    )
            continue
        expires_after = intent.expires_after_bars
        if expires_after is None or order.age_bars < expires_after:
            continue
        state.resting_orders.pop(client_order_id, None)
        broker = brokers.get(intent.venue) or brokers.get("*")
        if broker is not None:
            try:
                await broker.cancel(client_order_id)
            except Exception as exc:  # noqa: BLE001 - expiry is best effort in paper
                result.guard_events.append(
                    {
                        "kind": "limit_cancel_failed",
                        "client_order_id": client_order_id,
                        "reason": str(exc),
                        "timestamp": timestamp,
                    }
                )
        result.guard_events.append(
            {
                "kind": "limit_expired",
                "client_order_id": client_order_id,
                "symbol": intent.symbol,
                "age_bars": order.age_bars,
                "timestamp": timestamp,
            }
        )


def _drop_resting_orders(
    state: EngineState, *, symbol: str, reduce_only: bool | None = None
) -> None:
    for client_order_id, order in list(state.resting_orders.items()):
        if order.intent.symbol != symbol:
            continue
        if reduce_only is not None and order.intent.reduce_only != reduce_only:
            continue
        state.resting_orders.pop(client_order_id, None)


def _latest_visible_timestamp(view: CompletedBarsView) -> str | None:
    timestamps = view._ensure_timestamps()
    return timestamps[-1].isoformat() if timestamps else None


def _apply_engine_leverage(
    intents: list[OrderIntent], params: Mapping[str, Any]
) -> None:
    """Operator leverage knob, applied at the ONE seam backtest and live
    share. Scales unstamped, non-reduce-only intents by params.leverage so
    the knob works for every strategy with no strategy-code changes.
    Compound-mode strategies already multiply equity x leverage themselves
    and stamp `leverage_applied` — the stamp guarantees leverage is applied
    exactly once wherever the sizing happens. Defensive-inert: bad values
    never raise mid-tick (the knob validates its own range upstream)."""
    try:
        leverage = float(params.get("leverage") or 1.0)
    except (TypeError, ValueError):
        return
    if leverage == 1.0 or leverage <= 0:
        return
    for intent in intents:
        if intent.reduce_only or str(intent.action).upper() in REDUCE_ONLY_ACTIONS:
            continue
        if (intent.metadata or {}).get("leverage_applied"):
            continue
        if intent.notional is not None:
            intent.notional = float(intent.notional) * leverage
        if intent.size is not None:
            intent.size = float(intent.size) * leverage
        metadata = dict(intent.metadata or {})
        metadata["leverage_applied"] = True
        metadata["engine_leverage"] = leverage
        intent.metadata = metadata


def _record_fill(
    fill: FillEvent,
    *,
    intent: OrderIntent | None = None,
    state: EngineState,
    trace: ExecutionTrace,
    result: TickResult,
) -> None:
    realized_before = state.ledger.realized_pnl
    state.ledger.apply_fill(fill)
    trace.fills.append(fill.to_dict())
    result.fills.append(fill)
    if fill.successful:
        prune_closed_protection_groups(state)
        row = fill.to_dict()
        row["realized_pnl_delta"] = state.ledger.realized_pnl - realized_before
        result.trade_rows.append(row)
        position = state.ledger.positions.get(fill.symbol)
        if intent is not None and intent.bracket and not intent.reduce_only:
            if position is not None:
                state.brackets[fill.symbol] = _resolve_fill_bracket(
                    intent.bracket,
                    position.side,
                    position.avg_price,
                    intent.venue,
                    intent.client_order_id,
                )
        elif (
            fill.reduce_only
            and position is not None
            and intent is not None
            and intent.metadata.get("move_stop_to_break_even")
        ):
            bracket = state.brackets.get(fill.symbol)
            if bracket is not None:
                entry_price = float(bracket.get("entry_price") or position.avg_price)
                current_stop = _float_or_none(bracket.get("stop_loss"))
                bracket["stop_loss"] = (
                    max(current_stop or 0.0, entry_price)
                    if position.side == "long"
                    else min(current_stop or float("inf"), entry_price)
                )
        elif fill.reduce_only and position is None:
            state.brackets.pop(fill.symbol, None)
            _drop_resting_orders(state, symbol=fill.symbol, reduce_only=True)


async def _record_fill_and_protect(
    fill: FillEvent,
    *,
    intent: OrderIntent,
    brokers: Mapping[str, Broker],
    state: EngineState,
    trace: ExecutionTrace,
    result: TickResult,
    timestamp: str,
) -> None:
    previous_bracket = state.brackets.get(fill.symbol)
    _record_fill(fill, intent=intent, state=state, trace=trace, result=result)
    if not fill.successful or state.mode != "live":
        return

    if intent.reduce_only:
        await _sync_native_protection(
            brokers=brokers,
            state=state,
            symbol=fill.symbol,
            venue=intent.venue,
            result=result,
        )
        return

    bracket = state.brackets.get(fill.symbol) or {}
    if not bracket.get("native_required"):
        return
    installed = await _sync_native_protection(
        brokers=brokers,
        state=state,
        symbol=fill.symbol,
        venue=intent.venue,
        result=result,
    )
    if installed:
        return

    # A filled entry without a confirmed venue stop is not allowed to remain
    # open. Close only the newly filled risk; an older confirmed stop continues
    # to protect any pre-existing position.
    position = state.ledger.positions.get(fill.symbol)
    unwind_size = min(abs(fill.filled_size), position.size) if position else 0.0
    unwind_fill = None
    if position is not None and unwind_size > 0:
        unwind = OrderIntent(
            action="CLOSE",
            venue=intent.venue,
            symbol=fill.symbol,
            side="sell" if position.side == "long" else "buy",
            size=unwind_size,
            reduce_only=True,
            metadata={
                "exit_reason": "native_protection_unconfirmed",
                "position_side": position.side,
            },
        )
        unwind_fill = await _place(
            brokers, unwind, price=None, timestamp=timestamp, result=result
        )
        if unwind_fill is not None:
            _record_fill(
                unwind_fill,
                intent=unwind,
                state=state,
                trace=trace,
                result=result,
            )
    if previous_bracket is None:
        state.brackets.pop(fill.symbol, None)
    else:
        state.brackets[fill.symbol] = previous_bracket
    result.guard_events.append(
        {
            "kind": "native_protection_failed",
            "reason": "entry filled but venue stop could not be confirmed",
            "symbol": fill.symbol,
            "entry_client_order_id": fill.client_order_id,
            "unwind_status": unwind_fill.status if unwind_fill is not None else None,
            "halt_required": True,
            "timestamp": timestamp,
        }
    )


def _resolve_fill_bracket(
    policy: Mapping[str, Any],
    side: str,
    entry_price: float,
    venue: str,
    entry_client_order_id: str | None,
) -> dict[str, Any]:
    resolved = dict(policy)
    stop_pct = _float_or_none(resolved.get("stop_loss_pct"))
    if stop_pct is not None:
        direction = -1.0 if side == "long" else 1.0
        resolved["stop_loss"] = entry_price * (1.0 + direction * stop_pct)
    take_profit_pct = _float_or_none(resolved.get("take_profit_pct"))
    if take_profit_pct is not None:
        direction = 1.0 if side == "long" else -1.0
        resolved["take_profit"] = entry_price * (1.0 + direction * take_profit_pct)
    resolved["entry_price"] = entry_price
    resolved["entry_client_order_id"] = entry_client_order_id
    resolved["venue"] = venue
    return resolved


async def _sync_native_protection(
    *,
    brokers: Mapping[str, Broker],
    state: EngineState,
    symbol: str,
    venue: str,
    result: TickResult,
) -> bool:
    existing = state.native_protections.get(symbol)
    broker = brokers.get(venue) or brokers.get("*")
    if not isinstance(broker, NativeProtectionBroker):
        result.guard_events.append(
            {
                "kind": "native_protection_unsupported",
                "symbol": symbol,
                "venue": venue,
            }
        )
        return False

    position = state.ledger.positions.get(symbol)
    bracket = state.brackets.get(symbol) or {}
    if position is None or not bracket.get("native_required"):
        if existing:
            canceled = await broker.cancel_stop_loss(
                symbol=symbol,
                client_order_id=str(existing["client_order_id"]),
            )
            result.guard_events.append(
                {
                    "kind": "native_protection_canceled"
                    if canceled.confirmed
                    else "native_protection_cancel_unconfirmed",
                    "halt_required": not canceled.confirmed,
                    **canceled.to_dict(),
                }
            )
            if canceled.confirmed:
                state.native_protections.pop(symbol, None)
            return canceled.confirmed
        return True

    trigger_price = _float_or_none(bracket.get("stop_loss"))
    if trigger_price is None or trigger_price <= 0:
        return False
    generation = int((existing or {}).get("generation") or 0) + 1
    seed = (
        f"{bracket.get('entry_client_order_id')}|{symbol}|{position.side}|"
        f"{generation}|{trigger_price}|{position.size}"
    )
    client_order_id = f"0x{hashlib.sha256(seed.encode()).hexdigest()[:32]}"
    placed = await broker.place_stop_loss(
        symbol=symbol,
        side="sell" if position.side == "long" else "buy",
        size=position.size,
        trigger_price=trigger_price,
        client_order_id=client_order_id,
    )
    result.guard_events.append(
        {
            "kind": "native_protection_installed"
            if placed.confirmed
            else "native_protection_unconfirmed",
            **placed.to_dict(),
        }
    )
    if not placed.confirmed:
        return False

    state.native_protections[symbol] = {
        "venue": venue,
        "symbol": symbol,
        "side": position.side,
        "size": position.size,
        "trigger_price": trigger_price,
        "client_order_id": client_order_id,
        "order_id": placed.order_id,
        "generation": generation,
        "protection_group": bracket.get("protection_group"),
    }
    _record_protection_group(state, bracket.get("protection_group"))
    if existing and existing.get("client_order_id") != client_order_id:
        canceled = await broker.cancel_stop_loss(
            symbol=symbol,
            client_order_id=str(existing["client_order_id"]),
        )
        result.guard_events.append(
            {
                "kind": (
                    "native_protection_replaced"
                    if canceled.confirmed
                    else "native_protection_cancel_unconfirmed"
                ),
                "reason": (
                    None
                    if canceled.confirmed
                    else "replacement stop installed but prior stop cancellation "
                    "was not confirmed"
                ),
                "halt_required": not canceled.confirmed,
                **canceled.to_dict(),
            }
        )
    return True


def prune_closed_protection_groups(state: EngineState) -> None:
    """Drop recorded protection groups whose legs are ALL flat in the ledger.

    Anchors (entry_account_equity, entry_gross_notional) are recorded once
    per group id and must die with the position: without pruning, a re-entered
    pair reuses the FIRST entry's anchors forever, so the group loss budget
    is measured against stale equity/notional."""
    groups = state.strategy_state.get("protection_groups")
    if not groups:
        return
    for group_id in list(groups):
        group = groups[group_id]
        symbols = (
            [str(value) for value in group.get("symbols") or []]
            if isinstance(group, Mapping)
            else []
        )
        if symbols and all(symbol not in state.ledger.positions for symbol in symbols):
            groups.pop(group_id)


def _record_protection_group(
    state: EngineState, raw_group: Mapping[str, Any] | None
) -> None:
    if not raw_group:
        return
    group_id = str(raw_group.get("id") or "")
    symbols = [str(value) for value in raw_group.get("symbols") or []]
    if (
        not group_id
        or not symbols
        or any(symbol not in state.ledger.positions for symbol in symbols)
    ):
        return
    groups = state.strategy_state.setdefault("protection_groups", {})
    if group_id in groups:
        return
    entry_gross = sum(
        state.ledger.positions[symbol].size * state.ledger.positions[symbol].avg_price
        for symbol in symbols
    )
    groups[group_id] = {**dict(raw_group), "entry_gross_notional": entry_gross}


async def _place(
    brokers: Mapping[str, Broker],
    intent: OrderIntent,
    *,
    price: float | None,
    timestamp: str,
    result: TickResult,
) -> FillEvent | None:
    broker = brokers.get(intent.venue) or brokers.get("*")
    if broker is None:
        result.guard_events.append(
            {
                "kind": "no_broker_for_venue",
                "reason": f"no broker registered for venue {intent.venue!r}",
                "intent": intent.to_dict(),
                "timestamp": timestamp,
            }
        )
        return None
    return await broker.place(intent, timestamp=timestamp, price=price)


async def _evaluate_brackets(
    *,
    brokers: Mapping[str, Broker],
    state: EngineState,
    bars_by_symbol: Mapping[str, dict[str, Any]],
    timestamp: str,
    trace: ExecutionTrace,
    result: TickResult,
) -> None:
    for symbol, position in list(state.ledger.positions.items()):
        bracket = state.brackets.get(symbol)
        bar = bars_by_symbol.get(symbol)
        if not bracket or bar is None:
            continue
        if state.mode == "live" and symbol in state.native_protections:
            # The venue watches mark price continuously; evaluating a second
            # OHLC stop here would race and potentially double-submit a close.
            continue
        resolution = BracketEngine.resolve_intrabar(
            bar,
            position.side,
            _float_or_none(bracket.get("stop_loss")),
            _float_or_none(bracket.get("take_profit")),
            str(bracket.get("policy") or "conservative"),
        )
        trace.bracket_events.append(
            {"timestamp": timestamp, "symbol": symbol, **resolution}
        )
        if not resolution["hit"] or resolution["price"] is None:
            continue
        side = "sell" if position.side == "long" else "buy"
        intent = OrderIntent(
            action=resolution["exit_type"],
            venue=str(bracket.get("venue") or "backtest"),
            symbol=symbol,
            side=side,
            size=position.size,
            reduce_only=True,
            metadata={"bracket": resolution, "position_side": position.side},
        )
        fill = await _place(
            brokers,
            intent,
            price=float(resolution["price"]),
            timestamp=timestamp,
            result=result,
        )
        if fill is not None:
            await _record_fill_and_protect(
                fill,
                intent=intent,
                brokers=brokers,
                state=state,
                trace=trace,
                result=result,
                timestamp=timestamp,
            )
        if resolution["exit_type"] == "STOP_LOSS":
            cooldown_seconds = int(bracket.get("cooldown_seconds") or 0)
            if cooldown_seconds:
                cooldowns = state.strategy_state.setdefault("protection_cooldowns", {})
                cooldowns[symbol] = (
                    pd.Timestamp(timestamp) + pd.Timedelta(seconds=cooldown_seconds)
                ).isoformat()
        state.brackets.pop(symbol, None)
        _drop_resting_orders(state, symbol=symbol, reduce_only=True)


def _apply_market_event(
    event: MarketEvent,
    *,
    state: EngineState,
    trace: ExecutionTrace,
    result: TickResult,
    timestamp: str,
    reference_prices: Mapping[str, float],
) -> None:
    if event.kind == "funding":
        position = state.ledger.positions.get(event.symbol)
        rate = event.payload.get("rate")
        reference_price = event.payload.get("mark_price")
        if reference_price is None:
            reference_price = reference_prices.get(event.symbol)
        if "amount" in event.payload:
            amount = float(event.payload["amount"])
        elif position is not None and rate is not None and reference_price is not None:
            direction = 1.0 if position.side == "long" else -1.0
            amount = -direction * position.size * float(reference_price) * float(rate)
        else:
            amount = 0.0
        state.ledger.realized_pnl += amount
        result.guard_events.append(
            {
                "kind": "funding_applied",
                "symbol": event.symbol,
                "amount": amount,
                "rate": float(rate) if rate is not None else None,
                "reference_price": (
                    float(reference_price) if reference_price is not None else None
                ),
                "source_timestamp": event.payload.get("source_timestamp"),
                "timestamp": timestamp,
            }
        )
        return
    if event.kind == "resolution":
        position = state.ledger.positions.get(event.symbol)
        if position is None:
            return
        value = float(event.payload.get("value") or 0.0)
        fill = FillEvent(
            status="filled",
            venue=str(event.payload.get("venue") or "resolution"),
            symbol=event.symbol,
            side="sell" if position.side == "long" else "buy",
            filled_size=position.size,
            avg_price=value,
            reduce_only=True,
            raw={"market_event": event.to_dict()},
            timestamp=timestamp,
        )
        _record_fill(fill, state=state, trace=trace, result=result)
        state.brackets.pop(event.symbol, None)
        return
    if event.kind == "halt":
        result.guard_events.append(
            {"kind": "market_halt", "symbol": event.symbol, "timestamp": timestamp}
        )


async def _check_liquidation(
    *,
    brokers: Mapping[str, Broker],
    state: EngineState,
    bars_by_symbol: Mapping[str, Any],
    config: LiquidationConfig,
    timestamp: str,
    trace: ExecutionTrace,
    result: TickResult,
) -> bool:
    """Faithful port of the legacy total-wipe liquidation model
    (core/backtesting/backtester.py). Equity and maintenance requirement are
    computed at bar closes (legacy uses single per-bar prices; intrabar
    low/high is not checked). On breach every position is force-closed and
    equity pins to exactly 0 for the rest of the run."""
    equity = config.initial_capital + state.ledger.realized_pnl
    maintenance_requirement = 0.0
    for symbol, position in state.ledger.positions.items():
        bar = bars_by_symbol.get(symbol)
        close = bar.close if bar is not None else position.avg_price
        direction = 1 if position.side == "long" else -1
        equity += direction * (close - position.avg_price) * position.size
        if close > 0:
            maintenance_requirement += abs(position.size * close) * config.rate_for(
                symbol
            )
    breached = (
        equity > 0  # legacy gate: portfolio_value > 0
        and maintenance_requirement > 0
        and equity < maintenance_requirement * (1 + config.liquidation_buffer)
    )
    if not breached:
        return False
    for symbol, position in list(state.ledger.positions.items()):
        bar = bars_by_symbol.get(symbol)
        price = bar.close if bar is not None else position.avg_price
        intent = OrderIntent(
            action="CLOSE",
            venue=str((state.brackets.get(symbol) or {}).get("venue") or "backtest"),
            symbol=symbol,
            side="sell" if position.side == "long" else "buy",
            size=position.size,
            reduce_only=True,
            metadata={"liquidation": True, "position_side": position.side},
        )
        fill = await _place(
            brokers, intent, price=price, timestamp=timestamp, result=result
        )
        if fill is not None:
            _record_fill(fill, state=state, trace=trace, result=result)
    # Legacy total-wipe: pin equity to exactly 0 (the forced-close fills alone
    # would leave a fee/slippage residue around the maintenance level). Clear
    # positions unconditionally — a rejected forced-close fill must not leave
    # a phantom position marking against the wiped account.
    state.ledger.positions.clear()
    state.ledger.realized_pnl = -config.initial_capital
    state.liquidated_at = timestamp
    state.brackets = {}
    state.native_protections = {}
    state.pending_intents = []
    state.resting_orders = {}
    result.guard_events.append(
        {
            "kind": "liquidation",
            "equity": equity,
            "maintenance_requirement": maintenance_requirement,
            "timestamp": timestamp,
        }
    )
    return True


async def flatten_positions(
    *,
    brokers: Mapping[str, Broker],
    state: EngineState,
    view: CompletedBarsView,
    timestamp: str,
    trace: ExecutionTrace,
    result: TickResult,
) -> None:
    """Reduce-only CLOSE for every open position at the latest close, plus a
    cancel of every resting order. Used by the stale-data "flat" policy and by
    the manual kill switch (--flatten)."""
    for symbol, position in list(state.ledger.positions.items()):
        bracket = state.brackets.get(symbol) or {}
        # Venue resolution: bracket venue if routable; else the single
        # registered broker (the live driver keys brokers by real venue
        # names, and positions don't record theirs); else the "*" fallback
        # _place already honors.
        venue = str(bracket.get("venue") or "")
        if venue not in brokers:
            venue = next(iter(brokers)) if len(brokers) == 1 else "backtest"
        intent = OrderIntent(
            action="CLOSE",
            venue=venue,
            symbol=symbol,
            side="sell" if position.side == "long" else "buy",
            size=position.size,
            reduce_only=True,
            metadata={"stale_policy": "flat", "position_side": position.side},
        )
        price = (
            float(view.latest(symbol)["close"])
            if symbol in view.symbols
            else position.avg_price
        )
        fill = await _place(
            brokers, intent, price=price, timestamp=timestamp, result=result
        )
        if fill is not None:
            await _record_fill_and_protect(
                fill,
                intent=intent,
                brokers=brokers,
                state=state,
                trace=trace,
                result=result,
                timestamp=timestamp,
            )
        state.brackets.pop(symbol, None)
    # A flatten means "no exposure, no pending exposure": drop EVERY resting
    # order, not just reduce-only ones tied to the closed positions. The CLOSE
    # fills above already drop reduce-only TPs as a side effect, but
    # non-reduce-only entry limits (including for symbols with no open
    # position) would otherwise survive and fill after the halt. State-level
    # removal is authoritative; the broker cancel mirrors the best-effort
    # expiry cancel in _settle_resting_orders.
    for client_order_id, order in list(state.resting_orders.items()):
        state.resting_orders.pop(client_order_id, None)
        broker = brokers.get(order.intent.venue) or brokers.get("*")
        if broker is not None:
            try:
                await broker.cancel(client_order_id)
            except Exception as exc:  # noqa: BLE001 - cancel is best effort
                result.guard_events.append(
                    {
                        "kind": "limit_cancel_failed",
                        "client_order_id": client_order_id,
                        "reason": str(exc),
                        "timestamp": timestamp,
                    }
                )
        result.guard_events.append(
            {
                "kind": "limit_cancelled",
                "client_order_id": client_order_id,
                "symbol": order.intent.symbol,
                "reason": "flatten",
                "timestamp": timestamp,
            }
        )


def _validate_intent(
    intent: OrderIntent,
    *,
    brokers: Mapping[str, Broker],
    auto_limits: Mapping[str, Any] | None,
    state: EngineState,
    ref_price: float,
    bar_iso: str,
) -> str | None:
    if not intent.symbol:
        return "intent missing symbol"
    if intent.action == "OPEN" and not intent.side:
        return "OPEN intent missing side"

    broker = brokers.get(intent.venue) or brokers.get("*")
    capabilities = getattr(broker, "capabilities", None)
    if capabilities is not None:
        if intent.limit_price is not None and not capabilities.supports_limit_orders:
            return f"venue {intent.venue!r} does not support limit orders"
        if intent.bracket and not capabilities.supports_brackets:
            return (
                f"venue {intent.venue!r} does not support brackets; "
                "emit explicit CLOSE intents instead"
            )
        if (
            intent.action == "OPEN"
            and str(intent.side).lower() in OPEN_SIDES_SHORT
            and not capabilities.supports_shorts
        ):
            return f"venue {intent.venue!r} does not support short positions"
        if intent.notional is not None and not capabilities.supports_notional_sizing:
            return f"venue {intent.venue!r} requires explicit size, not notional"

    if intent.limit_price is not None:
        if state.mode == "live":
            return (
                "live limit orders require durable venue fill/cancel reconciliation; "
                "use paper or backtest mode"
            )
        if intent.limit_price <= 0:
            return "limit_price must be positive"
        time_in_force = str(intent.time_in_force or "").upper()
        if time_in_force != "ALO":
            return "paper/backtest limit orders require ALO time_in_force"
        intent.time_in_force = time_in_force
        if intent.expires_after_bars is not None and intent.expires_after_bars <= 0:
            return "expires_after_bars must be positive"
    elif intent.time_in_force is not None or intent.expires_after_bars is not None:
        return "time_in_force and expires_after_bars require limit_price"

    if not auto_limits:
        return None
    enabled_venues = auto_limits.get("enabled_venues")
    if enabled_venues and intent.venue not in enabled_venues:
        return f"venue {intent.venue!r} not in enabled_venues"
    allowed_symbols = auto_limits.get("allowed_symbols")
    if allowed_symbols and intent.symbol not in allowed_symbols:
        return f"symbol {intent.symbol!r} not in allowed_symbols"
    notional = _intent_notional(intent, ref_price)
    max_per_decision = _float_or_none(auto_limits.get("max_notional_per_decision"))
    if (
        max_per_decision is not None
        and notional is not None
        and not intent.reduce_only
        and notional > max_per_decision
    ):
        return f"notional {notional:.2f} exceeds max_notional_per_decision"
    max_daily = _float_or_none(auto_limits.get("max_daily_notional"))
    if max_daily is not None and notional is not None and not intent.reduce_only:
        day = bar_iso[:10]
        if state.daily_notional.get(day, 0.0) + notional > max_daily:
            return f"daily notional cap {max_daily:.2f} reached"
    max_positions = auto_limits.get("max_open_positions")
    if (
        max_positions is not None
        and intent.action == "OPEN"
        and intent.symbol not in state.ledger.positions
        and len(state.ledger.positions) >= int(max_positions)
    ):
        return f"max_open_positions {max_positions} reached"
    return None


def _intent_notional(intent: OrderIntent, ref_price: float) -> float | None:
    if intent.notional is not None:
        return abs(float(intent.notional))
    if intent.size is not None and ref_price:
        return abs(float(intent.size)) * ref_price
    return None


def _is_stale(
    bar_ts: pd.Timestamp, timestamp: pd.Timestamp, spec: ExecutionSpec
) -> str | None:
    bar_seconds = bar_interval_seconds(spec.data_contract.get("bar_interval"))
    if not bar_seconds:
        return None
    max_intervals = float(spec.data_contract.get("max_bar_age_intervals") or 2)
    age = (pd.Timestamp(timestamp) - pd.Timestamp(bar_ts)).total_seconds()
    if age > max_intervals * bar_seconds:
        return (
            f"latest completed bar is {age:.0f}s old "
            f"(max {max_intervals * bar_seconds:.0f}s)"
        )
    return None


def _bars_at_timestamp(view: CompletedBarsView, timestamp: Any) -> dict[str, Any]:
    bars: dict[str, Any] = {}
    for symbol in view.symbols:
        # row_at signals absence via ValueError (ragged multi-symbol views have
        # no membership test); this is lookup control flow, not a cast guard.
        try:
            bars[symbol] = view.row_at(timestamp, symbol=symbol)
        except ValueError:
            continue
    return bars
