from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from wayfinder_paths.jobs.execution import (
    CompletedBarsView,
    EngineState,
    ExecutionSpec,
    FillEvent,
    OrderIntent,
    PurityViolation,
    TradeCapacity,
    VenueCapabilities,
    VenueState,
    run_tick,
)
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
        params={},
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
    state.pending_intents.append(
        OrderIntent(
            action="OPEN", venue="hyperliquid", symbol="IMX", side="short", size=1
        )
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
    assert restored.pending_intents[0].symbol == "IMX"
    assert restored.last_processed_bar_ts == state.last_processed_bar_ts
    assert restored.daily_notional == {"2026-01-01": 20.0}
    assert restored.revision == "abc123"
