from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from wayfinder_paths.jobs.execution import (
    BacktestBroker,
    CompletedBarsView,
    EngineState,
    ExecutionSpec,
    FillEvent,
    NativeProtectionResult,
    OrderIntent,
    PurityViolation,
    RestingOrder,
    StateSnapshot,
    TradeCapacity,
    VenueCapabilities,
    VenueState,
    run_tick,
)
from wayfinder_paths.jobs.execution.primitives import PositionRecord
from wayfinder_paths.jobs.execution.venues import MarketEvent

PERP_CAPS = VenueCapabilities(
    market_kind="perp",
    supports_brackets=True,
    supports_shorts=True,
)
PREDICTION_CAPS = VenueCapabilities(
    market_kind="prediction",
    supports_brackets=False,
    supports_shorts=False,
    position_model="outcome_tokens",
    settlement="resolution",
)


class FakeBroker:
    def __init__(self, capabilities: VenueCapabilities = PERP_CAPS) -> None:
        self.capabilities = capabilities
        self.placed: list[OrderIntent] = []

    async def place(
        self, intent: OrderIntent, *, timestamp: str, price: float | None = None
    ) -> FillEvent:
        self.placed.append(intent)
        return FillEvent(
            status="filled",
            venue=intent.venue,
            symbol=intent.symbol,
            side=intent.side,
            filled_size=float(intent.size or 1.0),
            avg_price=float(price or 1.0),
            reduce_only=intent.reduce_only,
            raw={"intent_action": intent.action, "intent_metadata": intent.metadata},
            timestamp=timestamp,
        )

    async def fetch_state(self, symbols: Any = ()) -> VenueState:
        return VenueState(source="fake")

    async def get_capacity(self, symbol: str, side: str) -> TradeCapacity:
        return TradeCapacity(safe=True, source="fake")

    async def cancel(self, client_order_id: str) -> FillEvent:
        return FillEvent(
            status="rejected", venue="fake", symbol="", side="", error="unsupported"
        )


class FakeNativeBroker(FakeBroker):
    def __init__(self, *, confirm: bool = True, cancel_confirm: bool = True) -> None:
        super().__init__()
        self.confirm = confirm
        self.cancel_confirm = cancel_confirm
        self.stops: list[dict[str, Any]] = []

    async def place_stop_loss(self, **kwargs: Any) -> NativeProtectionResult:
        self.stops.append(kwargs)
        return NativeProtectionResult(
            status="confirmed" if self.confirm else "ambiguous",
            symbol=str(kwargs["symbol"]),
            client_order_id=str(kwargs["client_order_id"]),
        )

    async def cancel_stop_loss(self, **kwargs: Any) -> NativeProtectionResult:
        return NativeProtectionResult(
            status="confirmed" if self.cancel_confirm else "ambiguous",
            symbol=str(kwargs["symbol"]),
            client_order_id=str(kwargs["client_order_id"]),
        )


def _view(closes: list[float], symbol: str = "SNX") -> CompletedBarsView:
    rows = []
    for index, close in enumerate(closes):
        minute = index * 5
        rows.append(
            {
                "timestamp": f"2026-01-01T{minute // 60:02}:{minute % 60:02}:00Z",
                "symbol": symbol,
                "open": close - 0.1,
                "high": close + 0.5,
                "low": close - 0.5,
                "close": close,
                "volume": 10,
            }
        )
    return CompletedBarsView.from_rows(rows)


def _spec(**data_contract: Any) -> ExecutionSpec:
    spec = ExecutionSpec(fill_model="same_bar_close")
    spec.data_contract.update(data_contract)
    return spec


def _strategy(intents: list[Any]):
    def decide(ctx):
        return intents

    return decide


async def _tick(strategy, view, *, spec=None, timestamp=None, **kwargs):
    spec = spec or _spec()
    return await run_tick(
        strategy,
        view=view,
        brokers=kwargs.pop("brokers", {"*": FakeBroker()}),
        state=kwargs.pop("state", EngineState()),
        spec=spec,
        params=kwargs.pop("params", {}),
        timestamp=timestamp or view.timestamps[-1],
        **kwargs,
    )


async def test_purity_violation_on_wall_clock() -> None:
    def impure(ctx):
        import time

        time.time()
        return []

    with pytest.raises(PurityViolation):
        await _tick(impure, _view([10.0, 10.5]))


async def test_purity_can_be_disabled() -> None:
    def impure(ctx):
        import time

        time.time()
        return []

    result = await _tick(impure, _view([10.0, 10.5]), enforce_purity=False)
    assert result.skipped is False


async def test_auto_limits_block_oversized_and_off_list_intents() -> None:
    intents = [
        OrderIntent(
            action="OPEN", venue="hyperliquid", symbol="SNX", side="long", notional=500
        ),
        OrderIntent(
            action="OPEN", venue="hyperliquid", symbol="DOGE", side="long", notional=10
        ),
    ]
    result = await _tick(
        _strategy(intents),
        _view([10.0, 10.5]),
        auto_limits={
            "allowed_symbols": ["SNX"],
            "max_notional_per_decision": 100,
        },
    )

    assert result.intents == []
    reasons = [event["reason"] for event in result.guard_events]
    assert any("max_notional_per_decision" in reason for reason in reasons)
    assert any("allowed_symbols" in reason for reason in reasons)


async def test_symbol_block_and_risk_halt_preserve_semantic_close() -> None:
    broker = FakeBroker()
    state = EngineState()
    state.ledger.positions["SNX"] = PositionRecord(
        symbol="SNX", side="long", size=1.0, avg_price=10.0
    )
    close = OrderIntent(
        action="CLOSE",
        venue="hyperliquid",
        symbol="SNX",
        side="short",
        size=1.0,
        reduce_only=False,
    )

    result = await _tick(
        _strategy([close]),
        _view([10.0, 10.5]),
        brokers={"hyperliquid": broker},
        state=state,
        snapshot=StateSnapshot(status="risk_halt", reason="test"),
        blocked_entry_symbols={"SNX"},
        auto_limits={"max_notional_per_decision": 1.0},
    )

    assert broker.placed == [close]
    assert result.intents == [close]
    assert not any(event["kind"] == "intent_rejected" for event in result.guard_events)


async def test_semantic_stop_without_reduce_only_arms_defense_stand_down() -> None:
    class SemanticCloseBroker(FakeBroker):
        async def place(
            self, intent: OrderIntent, *, timestamp: str, price: float | None = None
        ) -> FillEvent:
            self.placed.append(intent)
            return FillEvent(
                status="filled",
                venue=intent.venue,
                symbol=intent.symbol,
                side="sell_close",
                filled_size=1.0,
                avg_price=float(price or 1.0),
                reduce_only=False,
                raw={
                    "intent_action": intent.action,
                    "intent_metadata": intent.metadata,
                },
                timestamp=timestamp,
            )

    state = EngineState()
    state.ledger.positions["SNX"] = PositionRecord(
        symbol="SNX", side="long", size=1.0, avg_price=10.0
    )
    stop = OrderIntent(
        action="STOP_LOSS",
        venue="hyperliquid",
        symbol="SNX",
        side="short",
        size=1.0,
        reduce_only=False,
    )

    result = await _tick(
        _strategy([stop]),
        _view([10.0, 9.0]),
        brokers={"hyperliquid": SemanticCloseBroker()},
        state=state,
        params={"defense_overlay": {"stop_loss_streak": 1}},
    )

    assert "SNX" in state.defense_state["stand_downs"]
    assert any(
        event["kind"] == "loss_streak_symbol_stand_down"
        for event in result.guard_events
    )


async def test_regime_contract_reports_target_without_blocking_entries() -> None:
    broker = FakeBroker()
    state = EngineState()
    state.ledger.positions["SNX"] = PositionRecord(
        symbol="SNX", side="long", size=1.0, avg_price=10.0
    )
    view = _view([10.0, 10.5])
    frame = view.to_frame()
    frame["__wf_portfolio_regime"] = "down_highvol"
    entry = OrderIntent(
        action="OPEN",
        venue="hyperliquid",
        symbol="SNX",
        side="long",
        size=1.0,
    )
    close = OrderIntent(
        action="CLOSE",
        venue="hyperliquid",
        symbol="SNX",
        side="short",
        size=1.0,
        reduce_only=False,
    )

    result = await _tick(
        _strategy([entry, close]),
        CompletedBarsView(frame),
        brokers={"hyperliquid": broker},
        state=state,
        params={"symbols": ["SNX"], "target_regimes": ["up_lowvol"]},
    )

    assert broker.placed == [entry, close]
    assert result.gates["portfolio_regime"] == {
        "current": "down_highvol",
        "target": ["up_lowvol"],
        "in_target_regime": False,
    }
    assert not any(event["kind"] == "intent_rejected" for event in result.guard_events)


async def test_ood_overlay_scales_entries_but_preserves_semantic_close() -> None:
    broker = FakeBroker()
    state = EngineState()
    state.ledger.positions["SNX"] = PositionRecord(
        symbol="SNX", side="long", size=2.0, avg_price=10.0
    )
    view = _view([10.0, 10.5])
    frame = view.to_frame()
    frame["__wf_ood_entry_scale"] = 0.25
    entry = OrderIntent(
        action="OPEN",
        venue="hyperliquid",
        symbol="SNX",
        side="long",
        notional=100.0,
    )
    close = OrderIntent(
        action="STOP_LOSS",
        venue="hyperliquid",
        symbol="SNX",
        side="short",
        size=2.0,
        reduce_only=False,
    )

    await _tick(
        _strategy([entry, close]),
        CompletedBarsView(frame),
        brokers={"hyperliquid": broker},
        state=state,
        params={"defense_overlay": {}},
    )

    assert broker.placed[0].notional == pytest.approx(25.0)
    assert broker.placed[1].size == pytest.approx(2.0)


async def test_daily_notional_cap_accumulates_across_ticks() -> None:
    state = EngineState()
    intent = OrderIntent(
        action="OPEN", venue="hyperliquid", symbol="SNX", side="long", notional=60
    )
    limits = {"max_daily_notional": 100}

    first = await _tick(
        _strategy([intent]), _view([10.0, 10.5]), state=state, auto_limits=limits
    )
    second = await _tick(
        _strategy([intent]), _view([10.0, 10.5, 11.0]), state=state, auto_limits=limits
    )

    assert len(first.intents) == 1
    assert second.intents == []
    assert any("daily notional cap" in event["reason"] for event in second.guard_events)


def test_backtest_broker_can_stress_stop_market_slippage_separately() -> None:
    broker = BacktestBroker(slippage_bps=10, stop_market_slippage_bps=1_000)
    ordinary = broker.execute(
        OrderIntent(
            action="OPEN",
            venue="hyperliquid",
            symbol="HYPE",
            side="buy",
            size=1,
        ),
        price=100,
        timestamp="2026-08-21T00:00:00+00:00",
    )
    stopped = broker.execute(
        OrderIntent(
            action="STOP_LOSS",
            venue="hyperliquid",
            symbol="HYPE",
            side="buy",
            size=1,
            reduce_only=True,
        ),
        price=100,
        timestamp="2026-08-21T00:05:00+00:00",
    )

    assert ordinary.avg_price == pytest.approx(100.1)
    assert stopped.avg_price == pytest.approx(110.0)
    assert stopped.raw["slippage_bps_applied"] == 1_000


async def test_passive_limit_rests_then_fills_on_next_bar_trade_through() -> None:
    state = EngineState()
    broker = BacktestBroker(fee_bps=4.5, maker_fee_bps=1.5, slippage_bps=3.5)
    intent = OrderIntent(
        action="OPEN",
        venue="hyperliquid",
        symbol="SNX",
        side="long",
        notional=95.0,
        limit_price=9.5,
        time_in_force="ALO",
        expires_after_bars=2,
    )

    first = await _tick(
        _strategy([intent]),
        _view([10.0, 10.0]),
        state=state,
        brokers={"hyperliquid": broker},
    )
    assert first.fills == []
    assert any(event["kind"] == "limit_resting" for event in first.guard_events)
    assert state.ledger.positions == {}
    assert len(state.resting_orders) == 1

    second = await _tick(
        _strategy([]),
        CompletedBarsView.from_rows(
            [
                *_view([10.0, 10.0]).to_rows(),
                {
                    "timestamp": "2026-01-01T00:10:00Z",
                    "symbol": "SNX",
                    "open": 10.0,
                    "high": 10.1,
                    "low": 9.4,
                    "close": 9.8,
                    "volume": 10,
                },
            ]
        ),
        state=state,
        brokers={"hyperliquid": broker},
    )

    assert second.fills[0].status == "filled"
    assert second.fills[0].avg_price == 9.5
    assert second.fills[0].fee == pytest.approx(95.0 * 0.00015)
    assert second.fills[0].raw["liquidity"] == "maker"
    assert state.ledger.positions["SNX"].size == pytest.approx(10.0)
    assert state.resting_orders == {}


async def test_passive_limit_touch_does_not_assume_queue_fill_and_expires() -> None:
    state = EngineState()
    broker = BacktestBroker(maker_fee_bps=1.5)
    intent = OrderIntent(
        action="OPEN",
        venue="hyperliquid",
        symbol="SNX",
        side="long",
        notional=95.0,
        limit_price=9.5,
        time_in_force="ALO",
        expires_after_bars=1,
    )
    await _tick(
        _strategy([intent]),
        _view([10.0, 10.0]),
        state=state,
        brokers={"hyperliquid": broker},
    )
    result = await _tick(
        _strategy([]),
        CompletedBarsView.from_rows(
            [
                *_view([10.0, 10.0]).to_rows(),
                {
                    "timestamp": "2026-01-01T00:10:00Z",
                    "symbol": "SNX",
                    "open": 10.0,
                    "high": 10.1,
                    "low": 9.5,
                    "close": 9.8,
                    "volume": 10,
                },
            ]
        ),
        state=state,
        brokers={"hyperliquid": broker},
    )

    assert state.ledger.positions == {}
    assert state.resting_orders == {}
    assert any(event["kind"] == "limit_expired" for event in result.guard_events)


async def test_live_limit_order_fails_closed_without_venue_reconciliation() -> None:
    state = EngineState(mode="live")
    broker = BacktestBroker(maker_fee_bps=1.5)
    result = await _tick(
        _strategy(
            [
                OrderIntent(
                    action="OPEN",
                    venue="hyperliquid",
                    symbol="SNX",
                    side="buy",
                    notional=100.0,
                    limit_price=9.5,
                    time_in_force="ALO",
                )
            ]
        ),
        _view([10.0, 10.0]),
        state=state,
        brokers={"hyperliquid": broker},
    )

    assert state.resting_orders == {}
    assert any(
        "durable venue fill/cancel reconciliation" in event["reason"]
        for event in result.guard_events
        if event["kind"] == "intent_rejected"
    )


async def test_paper_limit_order_requires_explicit_post_only_tif() -> None:
    state = EngineState()
    result = await _tick(
        _strategy(
            [
                OrderIntent(
                    action="OPEN",
                    venue="hyperliquid",
                    symbol="SNX",
                    side="buy",
                    notional=100.0,
                    limit_price=9.5,
                    time_in_force="GTC",
                )
            ]
        ),
        _view([10.0, 10.0]),
        state=state,
        brokers={"hyperliquid": BacktestBroker(maker_fee_bps=1.5)},
    )

    assert state.resting_orders == {}
    assert any(
        event["reason"] == "paper/backtest limit orders require ALO time_in_force"
        for event in result.guard_events
        if event["kind"] == "intent_rejected"
    )


async def test_partial_maker_exit_moves_remaining_stop_to_break_even() -> None:
    state = EngineState()
    state.ledger.positions["SNX"] = PositionRecord(
        symbol="SNX", side="long", size=10.0, avg_price=10.0
    )
    state.brackets["SNX"] = {
        "stop_loss": 9.0,
        "entry_price": 10.0,
        "venue": "hyperliquid",
    }
    intent = OrderIntent(
        action="TAKE_PROFIT",
        venue="hyperliquid",
        symbol="SNX",
        side="sell",
        size=5.0,
        reduce_only=True,
        limit_price=11.0,
        time_in_force="ALO",
        client_order_id="tp-one",
        metadata={"move_stop_to_break_even": True},
    )
    state.resting_orders["tp-one"] = RestingOrder(
        intent=intent,
        submitted_at="2026-01-01T00:05:00+00:00",
    )

    await _tick(
        _strategy([]),
        CompletedBarsView.from_rows(
            [
                {
                    "timestamp": "2026-01-01T00:10:00Z",
                    "symbol": "SNX",
                    "open": 10.5,
                    "high": 11.2,
                    "low": 10.4,
                    "close": 11.0,
                    "volume": 10,
                }
            ]
        ),
        state=state,
        brokers={"hyperliquid": BacktestBroker(maker_fee_bps=1.5)},
    )

    assert state.ledger.positions["SNX"].size == pytest.approx(5.0)
    assert state.brackets["SNX"]["stop_loss"] == pytest.approx(10.0)


async def test_stop_precedes_resting_take_profit_when_bar_crosses_both() -> None:
    state = EngineState()
    state.ledger.positions["SNX"] = PositionRecord(
        symbol="SNX", side="long", size=10.0, avg_price=10.0
    )
    state.brackets["SNX"] = {
        "stop_loss": 9.0,
        "entry_price": 10.0,
        "venue": "hyperliquid",
        "policy": "conservative",
    }
    take_profit = OrderIntent(
        action="TAKE_PROFIT",
        venue="hyperliquid",
        symbol="SNX",
        side="sell",
        size=10.0,
        reduce_only=True,
        limit_price=11.0,
        time_in_force="ALO",
        client_order_id="tp-full",
    )
    state.resting_orders["tp-full"] = RestingOrder(
        intent=take_profit,
        submitted_at="2026-01-01T00:05:00+00:00",
    )

    result = await _tick(
        _strategy([]),
        CompletedBarsView.from_rows(
            [
                {
                    "timestamp": "2026-01-01T00:10:00Z",
                    "symbol": "SNX",
                    "open": 10.0,
                    "high": 11.2,
                    "low": 8.8,
                    "close": 10.5,
                    "volume": 10,
                }
            ]
        ),
        state=state,
        brokers={"hyperliquid": BacktestBroker(maker_fee_bps=1.5)},
    )

    assert state.ledger.positions == {}
    assert state.resting_orders == {}
    assert len(result.fills) == 1
    assert result.fills[0].raw["intent_action"] == "STOP_LOSS"
    assert result.fills[0].avg_price == 9.0


async def test_stage_one_break_even_stop_precedes_second_target_same_bar() -> None:
    state = EngineState()
    state.ledger.positions["SNX"] = PositionRecord(
        symbol="SNX", side="long", size=10.0, avg_price=10.0
    )
    state.brackets["SNX"] = {
        "stop_loss": 9.0,
        "entry_price": 10.0,
        "venue": "hyperliquid",
        "policy": "conservative",
    }
    for client_order_id, limit_price, move_stop in (
        ("tp-one", 11.0, True),
        ("tp-two", 12.0, False),
    ):
        intent = OrderIntent(
            action="TAKE_PROFIT",
            venue="hyperliquid",
            symbol="SNX",
            side="sell",
            size=5.0,
            reduce_only=True,
            limit_price=limit_price,
            time_in_force="ALO",
            client_order_id=client_order_id,
            metadata={"move_stop_to_break_even": move_stop},
        )
        state.resting_orders[client_order_id] = RestingOrder(
            intent=intent,
            submitted_at="2026-01-01T00:05:00+00:00",
        )

    result = await _tick(
        _strategy([]),
        CompletedBarsView.from_rows(
            [
                {
                    "timestamp": "2026-01-01T00:10:00Z",
                    "symbol": "SNX",
                    "open": 10.5,
                    "high": 12.2,
                    "low": 9.8,
                    "close": 11.5,
                    "volume": 10,
                }
            ]
        ),
        state=state,
        brokers={"hyperliquid": BacktestBroker(maker_fee_bps=1.5)},
    )

    assert state.ledger.positions == {}
    assert state.resting_orders == {}
    assert [fill.raw["intent_action"] for fill in result.fills] == [
        "TAKE_PROFIT",
        "STOP_LOSS",
    ]
    assert [fill.avg_price for fill in result.fills] == [11.0, 10.0]


async def test_strategy_receives_isolated_copy_of_resting_order_state() -> None:
    state = EngineState()
    intent = OrderIntent(
        action="OPEN",
        venue="hyperliquid",
        symbol="OTHER",
        side="buy",
        notional=100.0,
        limit_price=9.5,
        time_in_force="ALO",
        client_order_id="entry-other",
    )
    state.resting_orders["entry-other"] = RestingOrder(
        intent=intent,
        submitted_at="2026-01-01T00:05:00+00:00",
    )

    def mutate_context(ctx):  # noqa: ANN001
        ctx.resting_orders[0].age_bars = 99
        ctx.resting_orders[0].intent.symbol = "MUTATED"
        return []

    await _tick(
        mutate_context,
        _view([10.0, 10.0]),
        state=state,
        brokers={"hyperliquid": BacktestBroker(maker_fee_bps=1.5)},
    )

    stored = state.resting_orders["entry-other"]
    assert stored.age_bars == 0
    assert stored.intent.symbol == "OTHER"


async def test_bracket_rejected_on_venue_without_bracket_support() -> None:
    broker = FakeBroker(capabilities=PREDICTION_CAPS)
    intent = OrderIntent(
        action="OPEN",
        venue="polymarket",
        symbol="polymarket:m1:YES",
        side="long",
        size=10,
        bracket={"stop_loss": 0.2},
    )

    result = await _tick(
        _strategy([intent]),
        _view([0.4, 0.45], symbol="polymarket:m1:YES"),
        brokers={"polymarket": broker},
    )

    assert result.intents == []
    assert broker.placed == []
    assert any(
        "does not support brackets" in event["reason"] for event in result.guard_events
    )


async def test_live_relative_stop_uses_fill_price_and_is_persisted() -> None:
    broker = FakeNativeBroker()
    state = EngineState(mode="live")
    intent = OrderIntent(
        action="OPEN",
        venue="hyperliquid",
        symbol="SNX",
        side="long",
        size=2.0,
        bracket={"stop_loss_pct": 0.05, "native_required": True},
    )

    await _tick(
        _strategy([intent]),
        _view([10.0, 10.5]),
        state=state,
        brokers={"hyperliquid": broker},
    )

    assert state.brackets["SNX"]["entry_price"] == 10.5
    assert state.brackets["SNX"]["stop_loss"] == pytest.approx(9.975)
    assert broker.stops[0]["trigger_price"] == pytest.approx(9.975)
    assert broker.stops[0]["size"] == 2.0
    assert state.native_protections["SNX"]["client_order_id"].startswith("0x")


async def test_unconfirmed_live_stop_unwinds_new_risk_and_requests_halt() -> None:
    broker = FakeNativeBroker(confirm=False)
    state = EngineState(mode="live")
    intent = OrderIntent(
        action="OPEN",
        venue="hyperliquid",
        symbol="SNX",
        side="long",
        size=1.0,
        bracket={"stop_loss_pct": 0.05, "native_required": True},
    )

    result = await _tick(
        _strategy([intent]),
        _view([10.0, 10.5]),
        state=state,
        brokers={"hyperliquid": broker},
    )

    assert "SNX" not in state.ledger.positions
    assert [intent.reduce_only for intent in broker.placed] == [False, True]
    failure = next(
        event
        for event in result.guard_events
        if event["kind"] == "native_protection_failed"
    )
    assert failure["halt_required"] is True


async def test_failed_unwind_preserves_required_protection_contract() -> None:
    class RejectingUnwindBroker(FakeNativeBroker):
        async def place(
            self, intent: OrderIntent, *, timestamp: str, price: float | None = None
        ) -> FillEvent:
            if intent.reduce_only:
                self.placed.append(intent)
                return FillEvent(
                    status="rejected",
                    venue=intent.venue,
                    symbol=intent.symbol,
                    side=intent.side,
                    reduce_only=True,
                    error="reduce_only_no_position",
                    raw={
                        "intent_action": intent.action,
                        "intent_metadata": intent.metadata,
                    },
                    timestamp=timestamp,
                )
            return await super().place(intent, timestamp=timestamp, price=price)

    broker = RejectingUnwindBroker(confirm=False)
    state = EngineState(mode="live")
    intent = OrderIntent(
        action="OPEN",
        venue="hyperliquid",
        symbol="SNX",
        side="long",
        size=1.0,
        bracket={"stop_loss_pct": 0.05, "native_required": True},
    )

    result = await _tick(
        _strategy([intent]),
        _view([10.0, 10.5]),
        state=state,
        brokers={"hyperliquid": broker},
    )

    assert "SNX" in state.ledger.positions
    assert state.brackets["SNX"]["native_required"] is True
    assert state.brackets["SNX"]["stop_loss"] == pytest.approx(9.975)
    failure = next(
        event
        for event in result.guard_events
        if event["kind"] == "native_protection_failed"
    )
    assert failure["unwind_status"] == "rejected"


async def test_unconfirmed_replaced_stop_cancel_requests_halt() -> None:
    broker = FakeNativeBroker(cancel_confirm=False)
    state = EngineState(mode="live")
    state.ledger.positions["SNX"] = PositionRecord(
        symbol="SNX", side="long", size=1.0, avg_price=10.0
    )
    state.brackets["SNX"] = {
        "stop_loss": 9.5,
        "native_required": True,
        "venue": "hyperliquid",
    }
    state.native_protections["SNX"] = {
        "venue": "hyperliquid",
        "client_order_id": "0x00000000000000000000000000000001",
        "generation": 1,
        "size": 1.0,
    }
    intent = OrderIntent(
        action="OPEN",
        venue="hyperliquid",
        symbol="SNX",
        side="long",
        size=1.0,
        bracket={"stop_loss_pct": 0.05, "native_required": True},
    )

    result = await _tick(
        _strategy([intent]),
        _view([10.0, 10.5]),
        state=state,
        brokers={"hyperliquid": broker},
    )

    event = next(
        item
        for item in result.guard_events
        if item["kind"] == "native_protection_cancel_unconfirmed"
    )
    assert event["halt_required"] is True
    assert state.native_protections["SNX"]["size"] == 2.0


async def test_short_rejected_on_long_only_venue() -> None:
    broker = FakeBroker(capabilities=PREDICTION_CAPS)
    intent = OrderIntent(
        action="OPEN",
        venue="polymarket",
        symbol="polymarket:m1:NO",
        side="short",
        size=5,
    )

    result = await _tick(
        _strategy([intent]),
        _view([0.4, 0.45], symbol="polymarket:m1:NO"),
        brokers={"polymarket": broker},
    )

    assert result.intents == []
    assert any(
        "does not support short" in event["reason"] for event in result.guard_events
    )


async def test_stale_data_skips_tick() -> None:
    view = _view([10.0, 10.5])
    late = view.timestamps[-1] + pd.Timedelta(minutes=30)

    result = await _tick(
        _strategy(
            [OrderIntent(action="OPEN", venue="x", symbol="SNX", side="long", size=1)]
        ),
        view,
        spec=_spec(bar_interval="5m", max_bar_age_intervals=2),
        timestamp=late,
    )

    assert result.skipped is True
    assert result.skip_reason == "stale_data"
    assert result.snapshot.status == "stale"
    assert result.intents == []


async def test_stale_decide_anyway_exposes_status_to_strategy() -> None:
    seen: dict[str, Any] = {}

    def decide(ctx):
        seen["status"] = ctx.state_snapshot.status
        return []

    view = _view([10.0, 10.5])
    result = await _tick(
        decide,
        view,
        spec=_spec(
            bar_interval="5m", max_bar_age_intervals=2, stale_policy="decide_anyway"
        ),
        timestamp=view.timestamps[-1] + pd.Timedelta(minutes=30),
    )

    assert result.skipped is False
    assert seen["status"] == "stale"


async def test_stale_flat_policy_closes_positions() -> None:
    broker = FakeBroker()
    state = EngineState()
    view = _view([10.0, 10.5])
    opener = await _tick(
        _strategy(
            [
                OrderIntent(
                    action="OPEN",
                    venue="hyperliquid",
                    symbol="SNX",
                    side="long",
                    size=2,
                )
            ]
        ),
        view,
        state=state,
        brokers={"*": broker},
    )
    assert opener.skipped is False
    assert "SNX" in state.ledger.positions

    stale_view = _view([10.0, 10.5, 11.0])
    result = await _tick(
        _strategy([]),
        stale_view,
        state=state,
        brokers={"*": broker},
        spec=_spec(bar_interval="5m", max_bar_age_intervals=2, stale_policy="flat"),
        timestamp=stale_view.timestamps[-1] + pd.Timedelta(minutes=30),
    )

    assert state.ledger.positions == {}
    assert any(fill.reduce_only for fill in result.fills)


async def test_flatten_cancels_resting_entry_alongside_position() -> None:
    state = EngineState()
    broker = BacktestBroker(maker_fee_bps=1.5)
    opener = await _tick(
        _strategy(
            [
                OrderIntent(
                    action="OPEN",
                    venue="hyperliquid",
                    symbol="SNX",
                    side="long",
                    size=2,
                ),
                OrderIntent(
                    action="OPEN",
                    venue="hyperliquid",
                    symbol="SNX",
                    side="long",
                    notional=95.0,
                    limit_price=9.5,
                    time_in_force="ALO",
                    expires_after_bars=5,
                ),
            ]
        ),
        _view([10.0, 10.0]),
        state=state,
        brokers={"hyperliquid": broker},
    )
    assert opener.skipped is False
    assert "SNX" in state.ledger.positions
    assert len(state.resting_orders) == 1

    stale_view = _view([10.0, 10.0, 10.0])
    result = await _tick(
        _strategy([]),
        stale_view,
        state=state,
        brokers={"hyperliquid": broker},
        spec=_spec(bar_interval="5m", max_bar_age_intervals=2, stale_policy="flat"),
        timestamp=stale_view.timestamps[-1] + pd.Timedelta(minutes=30),
    )

    assert state.ledger.positions == {}
    assert any(fill.reduce_only for fill in result.fills)
    assert state.resting_orders == {}
    assert state.to_dict()["resting_orders"] == {}
    assert any(
        event["kind"] == "limit_cancelled" and event["reason"] == "flatten"
        for event in result.guard_events
    )


async def test_flatten_cancels_resting_entry_without_position() -> None:
    state = EngineState()
    broker = BacktestBroker(maker_fee_bps=1.5)
    await _tick(
        _strategy(
            [
                OrderIntent(
                    action="OPEN",
                    venue="hyperliquid",
                    symbol="SNX",
                    side="long",
                    notional=95.0,
                    limit_price=9.5,
                    time_in_force="ALO",
                    expires_after_bars=5,
                )
            ]
        ),
        _view([10.0, 10.0]),
        state=state,
        brokers={"hyperliquid": broker},
    )
    assert state.ledger.positions == {}
    assert len(state.resting_orders) == 1

    stale_view = _view([10.0, 10.0, 10.0])
    flattened = await _tick(
        _strategy([]),
        stale_view,
        state=state,
        brokers={"hyperliquid": broker},
        spec=_spec(bar_interval="5m", max_bar_age_intervals=2, stale_policy="flat"),
        timestamp=stale_view.timestamps[-1] + pd.Timedelta(minutes=30),
    )

    assert flattened.fills == []
    assert state.resting_orders == {}
    assert state.to_dict()["resting_orders"] == {}
    assert any(
        event["kind"] == "limit_cancelled" and event["reason"] == "flatten"
        for event in flattened.guard_events
    )

    # This bar trades through the cancelled limit; it must not fill.
    third = await _tick(
        _strategy([]),
        CompletedBarsView.from_rows(
            [
                *stale_view.to_rows(),
                {
                    "timestamp": "2026-01-01T00:15:00Z",
                    "symbol": "SNX",
                    "open": 10.0,
                    "high": 10.1,
                    "low": 9.4,
                    "close": 9.8,
                    "volume": 10,
                },
            ]
        ),
        state=state,
        brokers={"hyperliquid": broker},
    )
    assert third.fills == []
    assert state.ledger.positions == {}


async def test_resolution_event_settles_outcome_token_position() -> None:
    broker = FakeBroker(capabilities=PREDICTION_CAPS)
    state = EngineState()
    symbol = "polymarket:m1:YES"
    opened = await _tick(
        _strategy(
            [
                OrderIntent(
                    action="OPEN",
                    venue="polymarket",
                    symbol=symbol,
                    side="long",
                    size=10,
                )
            ]
        ),
        _view([0.40, 0.42], symbol=symbol),
        state=state,
        brokers={"polymarket": broker},
    )
    assert opened.skipped is False
    entry_price = state.ledger.positions[symbol].avg_price

    result = await _tick(
        _strategy([]),
        _view([0.40, 0.42, 0.44], symbol=symbol),
        state=state,
        brokers={"polymarket": broker},
        events=[
            MarketEvent(
                kind="resolution",
                symbol=symbol,
                timestamp="2026-01-01T00:10:00+00:00",
                payload={"value": 1.0, "venue": "polymarket"},
            )
        ],
    )

    assert symbol not in state.ledger.positions
    assert state.ledger.realized_pnl == pytest.approx((1.0 - entry_price) * 10)
    assert any(row.get("raw", {}).get("market_event") for row in result.trade_rows)


async def test_multi_venue_routing() -> None:
    perp_broker = FakeBroker()
    prediction_broker = FakeBroker(capabilities=PREDICTION_CAPS)
    rows = (
        _view([10.0, 10.5]).to_rows() + _view([0.4, 0.45], symbol="pm:m1:YES").to_rows()
    )
    view = CompletedBarsView.from_rows(rows)

    result = await _tick(
        _strategy(
            [
                OrderIntent(
                    action="OPEN",
                    venue="hyperliquid",
                    symbol="SNX",
                    side="long",
                    size=1,
                ),
                OrderIntent(
                    action="OPEN",
                    venue="polymarket",
                    symbol="pm:m1:YES",
                    side="long",
                    size=5,
                ),
            ]
        ),
        view,
        brokers={"hyperliquid": perp_broker, "polymarket": prediction_broker},
    )

    assert result.skipped is False
    assert [intent.symbol for intent in perp_broker.placed] == ["SNX"]
    assert [intent.symbol for intent in prediction_broker.placed] == ["pm:m1:YES"]


async def test_duplicate_bar_is_idempotent() -> None:
    state = EngineState()
    broker = FakeBroker()
    view = _view([10.0, 10.5])
    intent = OrderIntent(
        action="OPEN", venue="hyperliquid", symbol="SNX", side="long", size=1
    )

    first = await _tick(_strategy([intent]), view, state=state, brokers={"*": broker})
    second = await _tick(_strategy([intent]), view, state=state, brokers={"*": broker})

    assert first.skipped is False
    assert second.skipped is True
    assert second.skip_reason == "no_new_bar"
    assert len(broker.placed) == 1


async def test_missing_broker_records_guard_event() -> None:
    result = await _tick(
        _strategy(
            [
                OrderIntent(
                    action="OPEN", venue="unknown", symbol="SNX", side="long", size=1
                )
            ]
        ),
        _view([10.0, 10.5]),
        brokers={"hyperliquid": FakeBroker()},
    )

    assert any(event["kind"] == "no_broker_for_venue" for event in result.guard_events)


async def test_strategy_state_persists_across_ticks() -> None:
    def decide(ctx):
        ctx.strategy_state["n"] = int(ctx.strategy_state.get("n") or 0) + 1
        return []

    state = EngineState()
    await _tick(decide, _view([10.0, 10.5]), state=state)
    await _tick(decide, _view([10.0, 10.5, 11.0]), state=state)

    assert state.strategy_state["n"] == 2
    assert state.to_dict()["strategy_state"] == {"n": 2}


def test_engine_state_strategy_state_roundtrip_and_backcompat() -> None:
    legacy = EngineState.from_dict({"ledger": {}, "brackets": {}})
    assert legacy.strategy_state == {}

    state = EngineState()
    state.strategy_state["rearm"] = True
    state.strategy_state["nested"] = {"since": "2026-01-01T00:00:00+00:00"}
    restored = EngineState.from_dict(state.to_dict())
    assert restored.strategy_state == state.strategy_state


def test_engine_state_round_trip(tmp_path) -> None:
    state = EngineState()
    state.ledger.apply_fill(
        FillEvent(
            status="filled",
            venue="hyperliquid",
            symbol="SNX",
            side="long",
            filled_size=2,
            avg_price=10,
            timestamp="2026-01-01T00:00:00+00:00",
        )
    )
    state.brackets["SNX"] = {"stop_loss": 9.0, "venue": "hyperliquid"}
    state.native_protections["SNX"] = {
        "client_order_id": "0x00000000000000000000000000000001",
        "trigger_price": 9.0,
    }
    state.pending_intents.append(
        OrderIntent(
            action="OPEN", venue="hyperliquid", symbol="IMX", side="short", size=1
        )
    )
    resting_intent = OrderIntent(
        action="OPEN",
        venue="hyperliquid",
        symbol="HYPE",
        side="long",
        size=1,
        limit_price=9.0,
        time_in_force="ALO",
        expires_after_bars=2,
        client_order_id="maker-entry-1",
    )
    state.resting_orders["maker-entry-1"] = RestingOrder(
        intent=resting_intent,
        submitted_at="2026-01-01T00:00:00+00:00",
        age_bars=1,
    )
    state.last_processed_bar_ts = "2026-01-01T00:00:00+00:00"
    state.daily_notional["2026-01-01"] = 20.0
    state.revision = "abc123"

    path = tmp_path / "state" / "engine_state.json"
    state.save(path)
    restored = EngineState.load(path)

    assert restored.ledger.positions["SNX"].size == 2
    assert restored.ledger.positions["SNX"].avg_price == 10
    assert restored.brackets["SNX"]["stop_loss"] == 9.0
    assert restored.native_protections["SNX"]["trigger_price"] == 9.0
    assert restored.pending_intents[0].symbol == "IMX"
    assert restored.resting_orders["maker-entry-1"].intent.time_in_force == "ALO"
    assert restored.resting_orders["maker-entry-1"].age_bars == 1
    assert restored.last_processed_bar_ts == state.last_processed_bar_ts
    assert restored.daily_notional == {"2026-01-01": 20.0}
    assert restored.revision == "abc123"


async def test_run_tick_records_strategy_state_digest_only_when_asked() -> None:
    from wayfinder_paths.jobs.execution.primitives import ExecutionTrace

    def decide(ctx):
        ctx.strategy_state["n"] = int(ctx.strategy_state.get("n") or 0) + 1
        ctx.strategy_state["fixed"] = "x"
        return []

    state = EngineState()
    plain = ExecutionTrace(execution_spec=_spec().to_dict())
    await _tick(decide, _view([10.0, 10.5]), state=state, trace=plain)
    assert "strategy_state_digest" not in plain.runs[-1]

    recorded = ExecutionTrace(execution_spec=_spec().to_dict())
    await _tick(
        decide,
        _view([10.0, 10.5, 11.0]),
        state=state,
        trace=recorded,
        record_strategy_state=True,
    )
    await _tick(
        decide,
        _view([10.0, 10.5, 11.0, 11.5]),
        state=state,
        trace=recorded,
        record_strategy_state=True,
    )
    first, second = (row["strategy_state_digest"] for row in recorded.runs[-2:])
    assert set(first) == {"n", "fixed"}
    assert first["n"] != second["n"] and first["fixed"] == second["fixed"]
