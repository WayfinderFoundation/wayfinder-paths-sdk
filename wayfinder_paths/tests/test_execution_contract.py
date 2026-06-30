from __future__ import annotations

import json
from pathlib import Path

import pytest

from wayfinder_paths.adapters.hyperliquid_adapter.adapter import HyperliquidAdapter
from wayfinder_paths.core.clients.HyperliquidDataClient import HyperliquidDataClient
from wayfinder_paths.jobs.execution import (
    BracketEngine,
    CompletedBarsView,
    ExecutionSpec,
    FillEvent,
    PositionLedger,
    StateSnapshot,
    get_trade_capacity,
    summarize_trade_capacity,
)
from wayfinder_paths.jobs.execution.job import backtest_execution_job, validate_job
from wayfinder_paths.jobs.execution.simulator import (
    PreparedExecutionDataset,
    simulate_execution,
)
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore


def _bars() -> list[dict]:
    return [
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "symbol": "SNX",
            "open": 10.0,
            "high": 10.8,
            "low": 9.8,
            "close": 10.5,
            "volume": 100,
        },
        {
            "timestamp": "2026-01-01T00:05:00Z",
            "symbol": "SNX",
            "open": 10.6,
            "high": 12.2,
            "low": 10.1,
            "close": 11.8,
            "volume": 150,
        },
    ]


def _write_strategy(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """
from __future__ import annotations

from wayfinder_paths.jobs.execution import OrderIntent


class Strategy:
    def __init__(self, params: dict):
        self.params = params

    def decide(self, ctx):
        latest = ctx.view.latest("SNX")
        threshold = float(self.params.get("threshold", 10.4))
        if not ctx.ledger.positions and float(latest["close"]) > threshold:
            return [
                OrderIntent(
                    action="OPEN",
                    venue="hyperliquid",
                    symbol="SNX",
                    side="long",
                    size=1,
                    bracket={"stop_loss": 9.5, "take_profit": 12.0},
                )
            ]
        return []


def build_strategy(params: dict) -> Strategy:
    return Strategy(params)
""".lstrip(),
        encoding="utf-8",
    )


def test_completed_bars_view_truncates_future_bars() -> None:
    view = CompletedBarsView.from_rows(_bars())

    first = view.through(0)

    assert len(first.to_frame()) == 1
    assert first.latest("SNX")["close"] == 10.5
    assert len(view.through(1).to_frame()) == 2


def test_bracket_engine_uses_high_low_for_long_and_short() -> None:
    bar = {"open": 10, "high": 12, "low": 8, "close": 11}

    assert BracketEngine.ohlc_stop_hit(bar, "long", 9)
    assert BracketEngine.ohlc_take_profit_hit(bar, "long", 11.5)
    assert BracketEngine.ohlc_stop_hit(bar, "short", 11.5)
    assert BracketEngine.ohlc_take_profit_hit(bar, "short", 9)
    assert (
        BracketEngine.resolve_intrabar(bar, "long", stop_loss=9, take_profit=11.5)[
            "exit_type"
        ]
        == "STOP_LOSS"
    )


def test_ledger_updates_only_from_fills_and_ticks_once() -> None:
    ledger = PositionLedger()
    ledger.on_bar_tick("t1")
    assert ledger.snapshot()["positions"] == {}

    ledger.apply_fill(
        FillEvent(
            status="filled",
            venue="backtest",
            symbol="SNX",
            side="long",
            filled_size=2,
            avg_price=10,
        )
    )
    ledger.on_bar_tick("t2")
    ledger.on_bar_tick("t2")

    assert ledger.snapshot()["positions"]["SNX"]["bars_held"] == 1


def test_state_snapshot_ambiguous_cannot_clear_state() -> None:
    assert StateSnapshot(status="ambiguous").usable_for_state_clear is False
    assert StateSnapshot(status="rate_limited").usable_for_state_clear is False
    assert StateSnapshot(status="valid").usable_for_state_clear is True


def test_simulate_execution_same_script_outputs_trace_and_visualization(
    tmp_path: Path,
) -> None:
    script = tmp_path / "strategy.py"
    _write_strategy(script)
    dataset = PreparedExecutionDataset.from_rows(_bars())

    result = simulate_execution(
        script,
        dataset,
        ExecutionSpec(),
        {"threshold": 10.4, "initial_capital": 1000},
    )

    assert result.validation["execution_valid"] is True
    assert result.stats["trade_count"] >= 2
    assert result.visualization["series"][0]["name"] == "equity"
    assert any(marker["kind"] == "entry" for marker in result.visualization["markers"])
    assert any(marker["kind"] == "exit" for marker in result.visualization["markers"])


def test_simulate_execution_uses_intent_symbol_bars(tmp_path: Path) -> None:
    script = tmp_path / "multi_strategy.py"
    script.write_text(
        """
from wayfinder_paths.jobs.execution import OrderIntent


def decide(ctx):
    latest = ctx.view.latest("IMX")
    if not ctx.ledger.positions and latest["close"] > 104:
        return [
            OrderIntent(
                action="OPEN",
                venue="hyperliquid",
                symbol="IMX",
                side="long",
                size=1,
                bracket={"take_profit": 128},
            )
        ]
    return []
""".lstrip(),
        encoding="utf-8",
    )
    rows = [
        {**_bars()[0], "symbol": "SNX"},
        {
            **_bars()[0],
            "symbol": "IMX",
            "open": 100,
            "high": 110,
            "low": 90,
            "close": 105,
        },
        {**_bars()[1], "symbol": "SNX"},
        {
            **_bars()[1],
            "symbol": "IMX",
            "open": 106,
            "high": 130,
            "low": 104,
            "close": 120,
        },
    ]

    result = simulate_execution(
        script, PreparedExecutionDataset.from_rows(rows), ExecutionSpec()
    )

    assert result.trades[0]["symbol"] == "IMX"
    assert result.trades[0]["avg_price"] == 106
    assert any(
        series.get("name") == "IMX close" and series.get("kind") == "market_price"
        for series in result.visualization["series"]
    )
    assert any(
        marker.get("label") == "TAKE_PROFIT"
        for marker in result.visualization["markers"]
    )


def test_job_backtest_and_validate_write_artifacts(tmp_path: Path) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "exec-demo",
        script=".wayfinder/jobs/exec-demo/workspace/src/strategy.py",
        interval_seconds=300,
    )
    job.execution_spec = ExecutionSpec().to_dict()
    store.save(job)
    root = store.job_dir(job.id)
    _write_strategy(root / "workspace" / "src" / "strategy.py")
    bars_path = root / "results" / "backtest" / "input_bars.json"
    bars_path.write_text(json.dumps(_bars()), encoding="utf-8")

    result = backtest_execution_job(job.id, store=store)
    validation = validate_job(job.id, strict=True, store=store)

    assert result["result"]["validation"]["execution_valid"] is True
    assert (root / "results" / "backtest" / "visualization.json").exists()
    assert validation["status"] == "passed"
    assert (root / "reports" / "validation" / "latest.json").exists()


@pytest.mark.asyncio
async def test_hyperliquid_data_client_accepts_lookback_hours(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"rows": []}

    async def fake_request(method: str, url: str, *, params: dict) -> FakeResponse:
        captured.update({"method": method, "url": url, "params": params})
        return FakeResponse()

    client = HyperliquidDataClient()
    monkeypatch.setattr(client, "_authed_request", fake_request)

    await client.get_candles("SNX", interval="5m", lookback_hours=6)

    params = captured["params"]
    match params:
        case {"start_ms": int(), "end_ms": int()}:
            pass
        case _:
            pytest.fail(f"unexpected candles params shape: {params!r}")
    assert params["coin"] == "SNX"
    assert params["interval"] == "5m"
    assert params["end_ms"] > params["start_ms"]


def test_trade_capacity_uses_active_asset_data_available_to_trade() -> None:
    capacity = summarize_trade_capacity(
        {
            "availableToTrade": ["10", "20"],
            "maxTradeSzs": ["0.25", "0.5"],
            "markPx": "100",
            "leverage": {"value": "5", "type": "cross"},
        },
        side="sell",
    )

    assert capacity.available_margin == 20
    assert capacity.max_position_size == 0.5
    assert capacity.max_notional == 50
    assert capacity.source == "activeAssetData.availableToTrade"


@pytest.mark.asyncio
async def test_trade_capacity_accepts_mcp_result_shape(monkeypatch) -> None:
    async def fake_get_trade_asset(label: str, asset_name: str) -> dict:
        assert label == "main"
        assert asset_name == "SNX-USDC"
        return {
            "ok": True,
            "result": {
                "raw": {
                    "availableToTrade": ["10", "20"],
                    "maxTradeSzs": ["0.25", "0.5"],
                    "markPx": "100",
                    "leverage": {"value": "5", "type": "cross"},
                }
            },
        }

    monkeypatch.setattr(
        "wayfinder_paths.mcp.tools.hyperliquid.hyperliquid_get_trade_asset",
        fake_get_trade_asset,
    )

    capacity = await get_trade_capacity("main", "SNX-USDC", side="buy")

    assert capacity.safe is True
    assert capacity.available_margin == 10
    assert capacity.max_notional == 25


@pytest.mark.asyncio
async def test_hyperliquid_trigger_order_passes_cloid(monkeypatch) -> None:
    adapter = HyperliquidAdapter()
    captured: dict[str, object] = {}

    async def noop(*args, **kwargs):  # noqa: ANN002, ANN003
        return None

    async def fake_broadcast(order_actions: dict, address: str) -> dict:
        captured["order_actions"] = order_actions
        captured["address"] = address
        return {
            "status": "ok",
            "response": {"data": {"statuses": [{"resting": {"oid": 1}}]}},
        }

    monkeypatch.setattr(adapter, "ensure_unified_account", noop)
    monkeypatch.setattr(adapter, "ensure_builder_fee_approved", noop)
    monkeypatch.setattr(adapter, "_mandatory_builder_fee", lambda builder: {})
    monkeypatch.setattr(adapter, "get_valid_order_price", lambda asset_id, price: price)
    monkeypatch.setattr(adapter, "_sign_and_broadcast_hypecore", fake_broadcast)

    ok, _ = await adapter.place_trigger_order(
        asset_id=1,
        is_buy=False,
        trigger_price=9.5,
        size=1.0,
        address="0x123",
        tpsl="sl",
        cloid="client-stop-1",
    )

    assert ok is True
    assert "client-stop-1" in json.dumps(captured["order_actions"])
