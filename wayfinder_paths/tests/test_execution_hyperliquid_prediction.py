from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from wayfinder_paths.jobs.execution import OrderIntent
from wayfinder_paths.jobs.execution.hyperliquid_prediction import (
    HYPERLIQUID_PREDICTION_CAPABILITIES,
    HyperliquidPredictionAdapter,
    HyperliquidPredictionBroker,
    HyperliquidPredictionFeed,
)
from wayfinder_paths.jobs.execution.paper import PaperBroker
from wayfinder_paths.jobs.execution.venues import VENUE_REGISTRY

HOUR_MS = 3_600_000
BASE_MS = 1_767_225_600_000  # 2026-01-01T00:00:00Z


def _candles(count: int, *, price: float = 0.15) -> list[dict[str, Any]]:
    rows = []
    for index in range(count):
        open_ms = BASE_MS + index * HOUR_MS
        p = price + index * 0.001
        rows.append(
            {
                "t": open_ms,
                "T": open_ms + HOUR_MS - 1,
                "o": str(p),
                "h": str(p + 0.002),
                "l": str(p - 0.002),
                "c": str(p + 0.001),
                "v": "100.0",
                "n": 5,
            }
        )
    return rows


class FakeCandleClient:
    def __init__(
        self, rows: list[dict[str, Any]] | None = None, *, error: bool = False
    ) -> None:
        self.rows = rows if rows is not None else []
        self.error = error
        self.calls = 0

    async def get_candles(
        self, coin, start_ms=None, end_ms=None, interval="1h", *, lookback_hours=None
    ):
        self.calls += 1
        if self.error:
            raise RuntimeError("gateway rejected #N coin")
        return list(self.rows)


def test_candles_to_completed_view_parses_ms_epochs_correctly() -> None:
    """Real HL candle rows carry ms-int close times; they must parse as 2026
    dates, not 1970 (pd.to_datetime without unit= reads ints as ns)."""
    from wayfinder_paths.jobs.execution.hyperliquid import (
        _candles_to_completed_view,
    )

    view = _candles_to_completed_view("#1730", _candles(3))
    frame = view.to_frame()

    assert frame["timestamp"].dt.year.min() == 2026
    assert frame["timestamp"].iloc[0] == pd.Timestamp(
        BASE_MS + HOUR_MS - 1, unit="ms", tz="UTC"
    )


async def test_feed_uses_gateway_when_it_returns_rows() -> None:
    gateway = FakeCandleClient(_candles(5))
    fallback = FakeCandleClient(_candles(5))
    feed = HyperliquidPredictionFeed(gateway, fallback=fallback)

    view = await feed.get_completed_bars(["#1730"], "1h", lookback_bars=10)

    assert len(view.to_frame()) == 5
    assert gateway.calls >= 1
    assert fallback.calls == 0
    assert view.symbols == ["#1730"]


@pytest.mark.parametrize(
    "gateway", [FakeCandleClient(error=True), FakeCandleClient([])]
)
async def test_feed_falls_back_on_error_or_empty(gateway: FakeCandleClient) -> None:
    fallback = FakeCandleClient(_candles(4))
    feed = HyperliquidPredictionFeed(gateway, fallback=fallback)

    view = await feed.get_completed_bars(["#1730"], "1h", lookback_bars=10)

    assert len(view.to_frame()) == 4
    assert fallback.calls == 1


async def test_feed_resolution_only_when_absent_and_terminal() -> None:
    async def live_assets() -> list[str]:
        return ["#40"]  # tracked symbols absent

    terminal = HyperliquidPredictionFeed(
        FakeCandleClient(_candles(3, price=0.97)),
        outcome_lister=live_assets,
    )
    mid = HyperliquidPredictionFeed(
        FakeCandleClient(_candles(3, price=0.50)),
        outcome_lister=live_assets,
    )
    still_live = HyperliquidPredictionFeed(
        FakeCandleClient(_candles(3, price=0.97)),
        outcome_lister=lambda: _async_return(["#1730"]),
    )

    resolved = await terminal.get_events(["#1730"])
    halted = await mid.get_events(["#1730"])
    none = await still_live.get_events(["#1730"])

    assert resolved[0].kind == "resolution"
    assert resolved[0].payload["value"] == 1.0
    assert halted[0].kind == "halt"
    assert none == []


async def _async_return(value):
    return value


def _intent(**overrides: Any) -> OrderIntent:
    payload = {
        "action": "OPEN",
        "venue": "hyperliquid_prediction",
        "symbol": "#1730",
        "side": "long",
        "size": 100,
    }
    payload.update(overrides)
    return OrderIntent.from_any(payload)


async def test_broker_rejects_fractional_contracts_and_small_orders() -> None:
    broker = HyperliquidPredictionBroker()

    fractional = await broker.place(_intent(size=10.5), timestamp="t0", price=0.5)
    tiny = await broker.place(_intent(size=10), timestamp="t0", price=0.5)
    tiny_notional = await broker.place(_intent(size=None, notional=5.0), timestamp="t0")

    assert fractional.status == "rejected"
    assert "integer" in fractional.error
    assert tiny.status == "rejected"  # 10 * 0.5 = $5 < $10 min
    assert tiny_notional.status == "rejected"


async def test_broker_places_market_order_and_parses_fill(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_place(**kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "result": {
                "effects": [
                    {
                        "type": "hl",
                        "label": "place_market_order",
                        "ok": True,
                        "result": {
                            "status": "ok",
                            "response": {
                                "data": {
                                    "statuses": [
                                        {
                                            "filled": {
                                                "totalSz": "100",
                                                "avgPx": "0.155",
                                                "oid": 42,
                                            }
                                        }
                                    ]
                                }
                            },
                        },
                    }
                ]
            },
        }

    monkeypatch.setattr(
        "wayfinder_paths.mcp.tools.hyperliquid.hyperliquid_place_market_order",
        fake_place,
    )
    broker = HyperliquidPredictionBroker(wallet_label="pm-bot")

    fill = await broker.place(_intent(size=100), timestamp="t0", price=0.15)

    assert fill.status == "filled"
    assert fill.filled_size == 100.0
    assert fill.avg_price == 0.155
    assert captured["asset_name"] == "#1730"
    assert captured["wallet_label"] == "pm-bot"
    assert captured["size"] == 100.0
    assert captured["reduce_only"] is False


async def test_broker_notional_passthrough_and_transport_ambiguous(monkeypatch) -> None:
    async def fake_place(**kwargs):
        raise RuntimeError("socket closed")

    monkeypatch.setattr(
        "wayfinder_paths.mcp.tools.hyperliquid.hyperliquid_place_market_order",
        fake_place,
    )
    broker = HyperliquidPredictionBroker()

    fill = await broker.place(_intent(size=None, notional=50.0), timestamp="t0")

    assert fill.status == "ambiguous"
    assert "socket closed" in fill.error


async def test_fetch_state_maps_outcome_balances(monkeypatch) -> None:
    async def fake_state(label):
        return {
            "ok": True,
            "result": {
                "outcomes": {
                    "positions": [
                        {
                            "coin": "+1730",
                            "outcome_id": 173,
                            "side": 0,
                            "total": "200",
                            "hold": "0",
                            "entryNtl": "30",
                        },
                        {"coin": "+40", "total": "0", "entryNtl": "0"},
                    ]
                }
            },
        }

    monkeypatch.setattr(
        "wayfinder_paths.mcp.tools.hyperliquid.hyperliquid_get_state", fake_state
    )
    broker = HyperliquidPredictionBroker()

    state = await broker.fetch_state(["#1730"])

    assert set(state.positions) == {"#1730"}
    record = state.positions["#1730"]
    assert record.size == 200.0
    assert record.avg_price == pytest.approx(0.15)
    assert record.side == "long"


def test_adapter_registration_and_paper_mode() -> None:
    assert "hyperliquid_prediction" in VENUE_REGISTRY
    adapter = HyperliquidPredictionAdapter(mode="paper", params={})
    assert type(adapter.broker) is PaperBroker
    assert adapter.broker.capabilities == HYPERLIQUID_PREDICTION_CAPABILITIES
    live = HyperliquidPredictionAdapter(mode="live", params={"wallet_label": "x"})
    assert type(live.broker) is HyperliquidPredictionBroker


async def test_engine_rejects_brackets_on_prediction_caps() -> None:
    from wayfinder_paths.jobs.execution import (
        CompletedBarsView,
        EngineState,
        ExecutionSpec,
        run_tick,
    )

    class CapOnlyBroker(PaperBroker):
        pass

    broker = CapOnlyBroker(capabilities=HYPERLIQUID_PREDICTION_CAPABILITIES)
    rows = [
        {
            "timestamp": pd.Timestamp(BASE_MS + i * HOUR_MS, unit="ms", tz="UTC"),
            "symbol": "#1730",
            "open": 0.15,
            "high": 0.16,
            "low": 0.14,
            "close": 0.15,
        }
        for i in range(2)
    ]
    view = CompletedBarsView.from_rows(rows)

    def decide(ctx):
        return [
            {
                "action": "OPEN",
                "venue": "hyperliquid_prediction",
                "symbol": "#1730",
                "side": "long",
                "size": 100,
                "bracket": {"stop_loss": 0.10},
            }
        ]

    result = await run_tick(
        decide,
        view=view,
        brokers={"hyperliquid_prediction": broker},
        state=EngineState(),
        spec=ExecutionSpec(fill_model="same_bar_close"),
        params={},
        timestamp=view.timestamps[-1],
    )

    assert result.intents == []
    assert any(
        "does not support brackets" in event["reason"] for event in result.guard_events
    )
