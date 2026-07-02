from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.execution.primitives import (
    DEFAULT_INITIAL_CAPITAL,
    BracketEngine,
    CompletedBarsView,
    ExecutionContext,
    ExecutionSpec,
    ExecutionTrace,
    FillEvent,
    OrderIntent,
    PositionLedger,
    StateSnapshot,
    TradeCapacity,
    _float_or_none,
    bar_interval_seconds,
)
from wayfinder_paths.jobs.execution.purity import purity_sandbox
from wayfinder_paths.jobs.execution.venues import Broker, MarketEvent

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
    pending_intents: list[OrderIntent] = field(default_factory=list)
    last_processed_bar_ts: str | None = None
    daily_notional: dict[str, float] = field(default_factory=dict)
    revision: str | None = None
    strategy_state: dict[str, Any] = field(default_factory=dict)
    liquidated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ledger": self.ledger.snapshot(),
            "brackets": dict(self.brackets),
            "pending_intents": [intent.to_dict() for intent in self.pending_intents],
            "last_processed_bar_ts": self.last_processed_bar_ts,
            "daily_notional": dict(self.daily_notional),
            "revision": self.revision,
            "strategy_state": dict(self.strategy_state),
            "liquidated_at": self.liquidated_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> EngineState:
        payload = data or {}
        return cls(
            ledger=PositionLedger.restore(payload.get("ledger")),
            brackets=dict(payload.get("brackets") or {}),
            pending_intents=[
                OrderIntent.from_any(item)
                for item in payload.get("pending_intents") or []
            ],
            last_processed_bar_ts=payload.get("last_processed_bar_ts"),
            daily_notional={
                str(key): float(value)
                for key, value in (payload.get("daily_notional") or {}).items()
            },
            revision=payload.get("revision"),
            strategy_state=dict(payload.get("strategy_state") or {}),
            liquidated_at=payload.get("liquidated_at"),
        )

    @classmethod
    def load(cls, path: str | Path) -> EngineState:
        location = Path(path)
        if not location.exists():
            return cls()
        return cls.from_dict(json.loads(location.read_text(encoding="utf-8")))

    def save(self, path: str | Path) -> None:
        location = Path(path)
        location.parent.mkdir(parents=True, exist_ok=True)
        location.write_text(
            json.dumps(self.to_dict(), indent=2, default=str) + "\n",
            encoding="utf-8",
        )


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
    default_bar = next(iter(bars_by_symbol.values()))

    state.ledger.on_bar_tick(bar_ts)

    for intent in state.pending_intents:
        bar = bars_by_symbol.get(intent.symbol, default_bar)
        fill = await _place(
            brokers, intent, price=bar.open, timestamp=bar_iso, result=result
        )
        if fill is not None:
            _record_fill(fill, state=state, trace=trace, result=result)
    state.pending_intents = []

    for event in events or []:
        _apply_market_event(
            event, state=state, trace=trace, result=result, timestamp=bar_iso
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
                    "guard_event_count": len(result.guard_events),
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

    for index, intent in enumerate(intents):
        if client_order_prefix and intent.client_order_id is None:
            # Deterministic per (job, bar, slot): an order submitted just before
            # a SIGKILL is recognized as ours on the next tick's fetch_state.
            seed = f"{client_order_prefix}|{bar_iso}|{index}"
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
        ref_price = bars_by_symbol.get(intent.symbol, default_bar).close
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
        result.intents.append(intent)
        if intent.bracket:
            state.brackets[intent.symbol] = {
                **dict(intent.bracket),
                "venue": intent.venue,
            }
        if not intent.reduce_only:
            notional = _intent_notional(intent, ref_price)
            if notional is not None:
                day = bar_iso[:10]
                state.daily_notional[day] = (
                    state.daily_notional.get(day, 0.0) + notional
                )
        if spec.fill_model == "next_bar_open":
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
                _record_fill(fill, state=state, trace=trace, result=result)

    state.last_processed_bar_ts = bar_iso
    ledger_snapshot = state.ledger.snapshot()
    result.ledger_snapshot = ledger_snapshot
    trace.ledger_snapshots.append({"timestamp": bar_iso, **ledger_snapshot})
    trace.runs.append(
        {
            "timestamp": bar_iso,
            # len(view) == row count; avoids a full DataFrame copy per bar.
            "visible_bar_count": len(ctx.view),
            "guard_event_count": len(result.guard_events),
        }
    )
    return result


def _record_fill(
    fill: FillEvent,
    *,
    state: EngineState,
    trace: ExecutionTrace,
    result: TickResult,
) -> None:
    realized_before = state.ledger.realized_pnl
    state.ledger.apply_fill(fill)
    trace.fills.append(fill.to_dict())
    result.fills.append(fill)
    if fill.successful:
        row = fill.to_dict()
        row["realized_pnl_delta"] = state.ledger.realized_pnl - realized_before
        result.trade_rows.append(row)


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
            _record_fill(fill, state=state, trace=trace, result=result)
        state.brackets.pop(symbol, None)


def _apply_market_event(
    event: MarketEvent,
    *,
    state: EngineState,
    trace: ExecutionTrace,
    result: TickResult,
    timestamp: str,
) -> None:
    if event.kind == "funding":
        amount = float(event.payload.get("amount") or 0.0)
        state.ledger.realized_pnl += amount
        result.guard_events.append(
            {
                "kind": "funding_applied",
                "symbol": event.symbol,
                "amount": amount,
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
    state.pending_intents = []
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
    """Reduce-only CLOSE for every open position at the latest close. Used by
    the stale-data "flat" policy and by the manual kill switch (--flatten)."""
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
            _record_fill(fill, state=state, trace=trace, result=result)
        state.brackets.pop(symbol, None)


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
