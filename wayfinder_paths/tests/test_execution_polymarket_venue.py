from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from wayfinder_paths.jobs.execution import ExecutionSpec, OrderIntent
from wayfinder_paths.jobs.execution.driver import tick_job
from wayfinder_paths.jobs.execution.engine import EngineState
from wayfinder_paths.jobs.execution.polymarket import (
    POLYMARKET_CAPABILITIES,
    PolymarketBroker,
    PolymarketMarketFeed,
    PolymarketResolver,
    parse_prediction_symbol,
)
from wayfinder_paths.jobs.execution.venues import VENUE_REGISTRY
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore

HOUR = 3600
BASE_TS = 1_767_225_600  # 2026-01-01T00:00:00Z (unix seconds)
SYMBOL = "polymarket:will-example-resolve:YES"

MARKET = {
    "slug": "will-example-resolve",
    "conditionId": "0xcond",
    "outcomes": ["Yes", "No"],
    "outcomePrices": [0.42, 0.58],
    "clobTokenIds": ["tok_yes", "tok_no"],
    "closed": False,
}


class FakePolymarketAdapter:
    def __init__(
        self,
        *,
        market: dict[str, Any] | None = None,
        history: list[dict[str, Any]] | None = None,
        positions: list[dict[str, Any]] | None = None,
        order_response: Any = None,
        order_ok: bool = True,
    ) -> None:
        self.market = dict(market or MARKET)
        self.history = history or []
        self.positions = positions or []
        self.order_response = order_response
        self.order_ok = order_ok
        self.market_fetches = 0
        self.orders: list[dict[str, Any]] = []
        self.cancelled: list[str] = []

    async def get_market_by_slug(self, slug: str) -> tuple[bool, Any]:
        self.market_fetches += 1
        return True, dict(self.market)

    async def get_market_by_condition_id(
        self, *, condition_id: str
    ) -> tuple[bool, Any]:
        self.market_fetches += 1
        return True, dict(self.market)

    def resolve_clob_token_id(self, *, market, outcome) -> tuple[bool, str]:
        outcomes = [str(o).lower() for o in market["outcomes"]]
        want = str(outcome).lower()
        if want in outcomes:
            return True, str(market["clobTokenIds"][outcomes.index(want)])
        return False, f"unknown outcome {outcome!r}"

    async def get_prices_history(self, **kwargs) -> tuple[bool, Any]:
        self.history_kwargs = kwargs
        return True, {"history": list(self.history)}

    async def get_price(self, *, token_id: str, side: str) -> tuple[bool, Any]:
        return True, {"price": "0.42"}

    async def get_order_book(self, *, token_id: str) -> tuple[bool, Any]:
        return True, {
            "asks": [{"price": "0.44", "size": "1000"}],
            "bids": [{"price": "0.40", "size": "800"}],
        }

    async def place_market_order(self, **kwargs) -> tuple[bool, Any]:
        self.orders.append({"kind": "market", **kwargs})
        if self.order_response is not None:
            return self.order_ok, self.order_response
        amount = float(kwargs["amount"])
        if kwargs["side"] == "BUY":
            shares = amount / 0.42
            return True, {
                "status": "matched",
                "orderID": "ord-1",
                "makingAmount": str(amount),
                "takingAmount": str(shares),
            }
        return True, {
            "status": "matched",
            "orderID": "ord-2",
            "makingAmount": str(amount),
            "takingAmount": str(amount * 0.42),
        }

    async def place_limit_order(self, **kwargs) -> tuple[bool, Any]:
        self.orders.append({"kind": "limit", **kwargs})
        return True, {"status": "live", "orderID": "ord-limit"}

    async def cancel_order(self, *, order_id: str) -> tuple[bool, Any]:
        self.cancelled.append(order_id)
        return True, {"canceled": order_id}

    async def get_positions(self, *, user: str, **_) -> tuple[bool, Any]:
        return True, list(self.positions)

    def deposit_wallet_address(self) -> str:
        return "0x00000000000000000000000000000000000000aa"


def _samples(count: int, *, price: float = 0.42) -> list[dict[str, Any]]:
    return [
        {"t": BASE_TS + index * HOUR + 120, "p": price + index * 0.001}
        for index in range(count)
    ]


def test_symbol_parsing() -> None:
    assert parse_prediction_symbol("polymarket:slug-a:YES") == ("slug-a", "YES")
    assert parse_prediction_symbol("polymarket:0xcond:Real Madrid") == (
        "0xcond",
        "Real Madrid",
    )
    with pytest.raises(ValueError):
        parse_prediction_symbol("hyperliquid:SNX")
    with pytest.raises(ValueError):
        parse_prediction_symbol("polymarket:only-market")


async def test_resolver_caches_market_lookup() -> None:
    adapter = FakePolymarketAdapter()
    resolver = PolymarketResolver(adapter)

    first = await resolver.resolve(SYMBOL)
    second = await resolver.resolve(SYMBOL)

    assert first["token_id"] == "tok_yes"
    assert first["outcome_index"] == 0
    assert second is first
    assert adapter.market_fetches == 1
    assert resolver.symbol_for_token("tok_yes") == SYMBOL


async def test_feed_builds_degenerate_bars_on_grid() -> None:
    adapter = FakePolymarketAdapter(history=_samples(5))
    feed = PolymarketMarketFeed(adapter)
    as_of = pd.Timestamp(BASE_TS + 4 * HOUR + 1800, unit="s", tz="UTC")

    view = await feed.get_completed_bars([SYMBOL], "1h", lookback_bars=10, as_of=as_of)

    frame = view.to_frame()
    # 5 samples; the 5th lands in an in-progress bucket -> dropped
    assert len(frame) == 4
    for _, row in frame.iterrows():
        assert row["open"] == row["high"] == row["low"] == row["close"]
        assert int(row["timestamp"].timestamp()) % HOUR == 0
    assert adapter.history_kwargs["fidelity"] == 60
    assert adapter.history_kwargs["interval"] is None


async def test_feed_emits_resolution_event_for_closed_market() -> None:
    resolved = {**MARKET, "closed": True, "outcomePrices": [1.0, 0.0]}
    adapter = FakePolymarketAdapter(market=resolved)
    feed = PolymarketMarketFeed(adapter)

    yes_events = await feed.get_events([SYMBOL])
    no_events = await feed.get_events(["polymarket:will-example-resolve:NO"])

    assert yes_events[0].kind == "resolution"
    assert yes_events[0].payload["value"] == 1.0
    assert no_events[0].payload["value"] == 0.0


async def test_feed_no_event_for_open_market() -> None:
    feed = PolymarketMarketFeed(FakePolymarketAdapter())

    assert await feed.get_events([SYMBOL]) == []


def _intent(**overrides: Any) -> OrderIntent:
    payload = {
        "action": "OPEN",
        "venue": "polymarket",
        "symbol": SYMBOL,
        "side": "long",
        "notional": 100.0,
        "client_order_id": "cloid-1",
    }
    payload.update(overrides)
    return OrderIntent.from_any(payload)


async def test_broker_buy_notional_fills_in_shares() -> None:
    adapter = FakePolymarketAdapter()
    broker = PolymarketBroker(adapter)

    fill = await broker.place(_intent(), timestamp="t0")

    assert fill.status == "filled"
    assert fill.filled_size == pytest.approx(100.0 / 0.42)
    assert fill.avg_price == pytest.approx(0.42)
    assert fill.avg_price * fill.filled_size == pytest.approx(100.0)
    assert adapter.orders[0]["side"] == "BUY"
    assert adapter.orders[0]["amount"] == 100.0


async def test_broker_close_sells_shares() -> None:
    adapter = FakePolymarketAdapter()
    broker = PolymarketBroker(adapter)

    fill = await broker.place(
        _intent(
            action="CLOSE", side="sell", notional=None, size=50.0, reduce_only=True
        ),
        timestamp="t0",
    )

    assert fill.status == "filled"
    assert adapter.orders[0]["side"] == "SELL"
    assert adapter.orders[0]["amount"] == 50.0
    assert fill.filled_size == pytest.approx(50.0)  # makingAmount = shares


async def test_broker_limit_order_rests_and_cancels() -> None:
    adapter = FakePolymarketAdapter()
    broker = PolymarketBroker(adapter)

    fill = await broker.place(
        _intent(limit_price=0.40, size=100.0, notional=None), timestamp="t0"
    )
    cancel = await broker.cancel("cloid-1")
    unknown = await broker.cancel("nope")

    assert fill.status == "resting"
    assert fill.order_id == "ord-limit"
    assert cancel.status == "filled"
    assert adapter.cancelled == ["ord-limit"]
    assert unknown.status == "rejected"


async def test_broker_error_dict_rejected_and_transport_ambiguous() -> None:
    rejected = PolymarketBroker(
        FakePolymarketAdapter(
            order_ok=False,
            order_response={"error": "insufficient book liquidity"},
        )
    )
    ambiguous = PolymarketBroker(
        FakePolymarketAdapter(order_ok=False, order_response=None)
    )
    ambiguous.adapter.order_response = None

    async def _boom(**kwargs):
        raise RuntimeError("connection reset")

    ambiguous.adapter.place_market_order = _boom

    reject_fill = await rejected.place(_intent(), timestamp="t0")
    ambiguous_fill = await ambiguous.place(_intent(), timestamp="t0")

    assert reject_fill.status == "rejected"
    assert "liquidity" in reject_fill.error
    assert ambiguous_fill.status == "ambiguous"


async def test_broker_fetch_state_maps_tokens_to_symbols() -> None:
    adapter = FakePolymarketAdapter(
        positions=[
            {
                "asset": "tok_yes",
                "size": "120",
                "avgPrice": "0.35",
                "redeemable": False,
            },
            {"asset": "tok_other", "size": "50", "avgPrice": "0.5"},
        ]
    )
    broker = PolymarketBroker(adapter)

    state = await broker.fetch_state([SYMBOL])

    assert set(state.positions) == {SYMBOL}
    record = state.positions[SYMBOL]
    assert record.size == 120.0
    assert record.avg_price == 0.35


def test_registered_in_venue_registry() -> None:
    assert "polymarket" in VENUE_REGISTRY


async def test_end_to_end_resolution_settles_position(tmp_path: Path) -> None:
    """Toy strategy buys YES below 0.45 and holds; the market resolves YES ->
    the engine settles the outcome-token position at 1.0 via the feed's
    resolution event, booking (1.0 - entry) * shares."""
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "pm-demo",
        script=".wayfinder/jobs/pm-demo/workspace/src/strategy.py",
        interval_seconds=3600,
        execution_contract="jobs_v1",
    )
    spec = ExecutionSpec(market_kind="prediction", fill_model="same_bar_close")
    spec.data_contract["bar_interval"] = "1h"
    job.execution_spec = spec.to_dict()
    job.execution_params = {"symbols": [SYMBOL]}
    store.save(job)
    root = store.job_dir(job.id)
    script = root / "workspace" / "src" / "strategy.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        f"""
def decide(ctx):
    symbol = {SYMBOL!r}
    if ctx.strategy_state.get("entered"):
        return []
    latest = ctx.view.latest(symbol)
    if symbol not in ctx.ledger.positions and float(latest["close"]) < 0.45:
        ctx.strategy_state["entered"] = True
        return [
            {{
                "action": "OPEN",
                "venue": "polymarket",
                "symbol": symbol,
                "side": "long",
                "notional": 100.0,
            }}
        ]
    return []
""".lstrip(),
        encoding="utf-8",
    )

    adapter = FakePolymarketAdapter(history=_samples(4))
    resolver = PolymarketResolver(adapter)
    feed = PolymarketMarketFeed(adapter, resolver=resolver)
    broker = PolymarketBroker(adapter, resolver=resolver)

    class Venue:
        name = "polymarket"
        capabilities = POLYMARKET_CAPABILITIES

        def __init__(self) -> None:
            self.feed = feed
            self.broker = broker

    venue = Venue()
    first_now = pd.Timestamp(BASE_TS + 3 * HOUR, unit="s", tz="UTC")
    first = await tick_job(
        job, root, "live", store=store, adapters={"polymarket": venue}, now=first_now
    )
    assert first["ok"] is True
    assert first["fills"], "entry should fill same-bar"
    entry_price = first["fills"][0]["avg_price"]
    shares = first["fills"][0]["filled_size"]
    adapter.positions = [
        {"asset": "tok_yes", "size": str(shares), "avgPrice": str(entry_price)}
    ]

    adapter.market = {**MARKET, "closed": True, "outcomePrices": [1.0, 0.0]}
    adapter.history = _samples(5)
    second_now = pd.Timestamp(BASE_TS + 4 * HOUR, unit="s", tz="UTC")
    second = await tick_job(
        job, root, "live", store=store, adapters={"polymarket": venue}, now=second_now
    )

    assert second["ok"] is True
    settle_fills = [f for f in second["fills"] if f["reduce_only"]]
    assert settle_fills and settle_fills[0]["avg_price"] == 1.0
    state = EngineState.load(root / "state" / "engine_state.json")
    assert SYMBOL not in state.ledger.positions
    assert state.ledger.realized_pnl == pytest.approx((1.0 - entry_price) * shares)
    ticks = (root / "results" / "forward" / "ticks.jsonl").read_text().splitlines()
    assert len(json.loads(ticks[-1])["fills"]) >= 1
