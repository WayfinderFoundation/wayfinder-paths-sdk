from __future__ import annotations

import asyncio
import importlib.util
import itertools
import json
import sys
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.execution.primitives import (
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
)
from wayfinder_paths.jobs.execution.validation import validate_execution_trace

REDUCE_ONLY_ACTIONS = frozenset({"CLOSE", "STOP_LOSS", "TAKE_PROFIT"})


@dataclass
class PreparedExecutionDataset:
    bars: CompletedBarsView
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_rows(
        cls, rows: list[Mapping[str, Any]], metadata: Mapping[str, Any] | None = None
    ) -> PreparedExecutionDataset:
        return cls(CompletedBarsView.from_rows(rows), dict(metadata or {}))

    def to_dict(self) -> dict[str, Any]:
        return {"bars": self.bars.to_rows(), "metadata": self.metadata}


@dataclass
class ExecutionBacktestResult:
    run_id: str
    params: dict[str, Any]
    equity_curve: list[dict[str, Any]]
    trades: list[dict[str, Any]]
    positions: list[dict[str, Any]]
    stats: dict[str, Any]
    trace: dict[str, Any]
    validation: dict[str, Any]
    visualization: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExecutionGridResult:
    grid_id: str
    rank_by: str
    runs: list[dict[str, Any]]
    ranked: list[dict[str, Any]]
    invalid: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BacktestBroker:
    def __init__(self, *, fee_bps: float = 0.0, slippage_bps: float = 0.0) -> None:
        self.fee_bps = float(fee_bps)
        self.slippage_bps = float(slippage_bps)

    def execute(
        self, intent: OrderIntent, *, price: float, timestamp: str
    ) -> FillEvent:
        if not intent.symbol:
            return FillEvent(
                status="rejected",
                venue=intent.venue,
                symbol=intent.symbol,
                side=intent.side,
                error="symbol is required",
                client_order_id=intent.client_order_id,
                timestamp=timestamp,
            )
        size = intent.size
        if size is None and intent.notional is not None and price > 0:
            size = abs(float(intent.notional)) / price
        if size is None or size <= 0:
            return FillEvent(
                status="rejected",
                venue=intent.venue,
                symbol=intent.symbol,
                side=intent.side,
                error="positive size or notional is required",
                client_order_id=intent.client_order_id,
                timestamp=timestamp,
            )
        side_multiplier = 1 if str(intent.side).lower() in {"buy", "long"} else -1
        fill_price = price * (1 + side_multiplier * self.slippage_bps / 10_000)
        fee = abs(size * fill_price) * self.fee_bps / 10_000
        return FillEvent(
            status="filled",
            venue=intent.venue,
            symbol=intent.symbol,
            side=intent.side,
            filled_size=float(size),
            avg_price=float(fill_price),
            fee=float(fee),
            client_order_id=intent.client_order_id,
            reduce_only=intent.reduce_only or intent.action in REDUCE_ONLY_ACTIONS,
            raw={
                "intent_action": intent.action,
                "intent_metadata": intent.metadata,
                "bracket": intent.bracket,
            },
            timestamp=timestamp,
        )


def simulate_execution(
    script_entrypoint: str | Path | Callable[..., Any],
    dataset: PreparedExecutionDataset,
    execution_spec: ExecutionSpec | Mapping[str, Any] | None = None,
    params: Mapping[str, Any] | None = None,
) -> ExecutionBacktestResult:
    match execution_spec:
        case ExecutionSpec():
            spec = execution_spec
        case _:
            spec = ExecutionSpec.from_dict(execution_spec)
    params_data = dict(params or {})
    strategy = _load_strategy(script_entrypoint, params_data)
    broker = BacktestBroker(
        fee_bps=float(params_data.get("fee_bps") or 0.0),
        slippage_bps=float(params_data.get("slippage_bps") or 0.0),
    )
    ledger = PositionLedger()
    trace = ExecutionTrace(execution_spec=spec.to_dict())
    pending: list[OrderIntent] = []
    trades: list[dict[str, Any]] = []
    positions: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    price_series: dict[str, list[dict[str, Any]]] = {
        symbol: [] for symbol in dataset.bars.symbols
    }
    brackets: dict[str, dict[str, Any]] = {}
    initial_capital = float(params_data.get("initial_capital") or 10_000.0)

    timestamps = dataset.bars.timestamps
    for index, timestamp in enumerate(timestamps):
        bars_by_symbol = _bars_at_timestamp(dataset.bars, timestamp)
        if not bars_by_symbol:
            continue
        default_bar = next(iter(bars_by_symbol.values()))
        for symbol, bar in bars_by_symbol.items():
            price_series.setdefault(symbol, []).append(
                {"timestamp": timestamp.isoformat(), "value": bar.close}
            )
        ledger.on_bar_tick(timestamp)

        fills = [
            broker.execute(
                intent,
                price=bars_by_symbol.get(intent.symbol, default_bar).open,
                timestamp=timestamp.isoformat(),
            )
            for intent in pending
        ]
        pending = []
        for fill in fills:
            ledger.apply_fill(fill)
            trace.fills.append(fill.to_dict())
            if fill.successful:
                trades.append(fill.to_dict())
        _evaluate_brackets(
            ledger=ledger,
            brackets=brackets,
            bars_by_symbol={
                symbol: bar.to_dict() for symbol, bar in bars_by_symbol.items()
            },
            broker=broker,
            timestamp=timestamp.isoformat(),
            trace=trace,
            trades=trades,
        )

        ctx = ExecutionContext(
            view=dataset.bars.through(index),
            ledger=ledger,
            state_snapshot=StateSnapshot(status="valid"),
            capacity=TradeCapacity(
                max_notional=float(params_data.get("max_notional") or 1_000_000.0),
                available_margin=float(
                    params_data.get("available_margin") or 1_000_000.0
                ),
                max_position_size=None,
                safe=True,
                source="backtest_fixture",
            ),
            params=params_data,
            timestamp=timestamp.isoformat(),
            execution_spec=spec,
        )
        intents = [OrderIntent.from_any(item) for item in _call_decide(strategy, ctx)]
        for intent in intents:
            trace.intents.append(
                {"timestamp": timestamp.isoformat(), **intent.to_dict()}
            )
            if intent.bracket:
                brackets[intent.symbol] = dict(intent.bracket)
            if spec.fill_model == "next_bar_open":
                pending.append(intent)
            else:
                intent_bar = bars_by_symbol.get(intent.symbol, default_bar)
                fill = broker.execute(
                    intent,
                    price=intent_bar.close,
                    timestamp=timestamp.isoformat(),
                )
                ledger.apply_fill(fill)
                trace.fills.append(fill.to_dict())
                if fill.successful:
                    trades.append(fill.to_dict())

        ledger_snapshot = ledger.snapshot()
        trace.ledger_snapshots.append(
            {"timestamp": timestamp.isoformat(), **ledger_snapshot}
        )
        positions.append({"timestamp": timestamp.isoformat(), **ledger_snapshot})
        mark_to_market = _mark_to_market(
            ledger, {symbol: bar.close for symbol, bar in bars_by_symbol.items()}
        )
        equity_curve.append(
            {
                "timestamp": timestamp.isoformat(),
                "equity": initial_capital + ledger.realized_pnl + mark_to_market,
                "realized_pnl": ledger.realized_pnl,
                "unrealized_pnl": mark_to_market,
            }
        )
        trace.runs.append(
            {
                "timestamp": timestamp.isoformat(),
                "visible_bar_count": len(ctx.view.to_frame()),
            }
        )

    validation = validate_execution_trace(trace.to_dict(), spec)
    stats = _stats(equity_curve, trades)
    visualization = {
        "schema_version": "1.0",
        "market_kind": spec.market_kind,
        "view_type": spec.view_type,
        "symbols": dataset.bars.symbols,
        "series": [
            {"name": "equity", "kind": "equity_curve", "points": equity_curve},
            *[
                {
                    "name": f"{symbol} close",
                    "kind": "market_price",
                    "symbol": symbol,
                    "points": points,
                }
                for symbol, points in price_series.items()
            ],
        ],
        "markers": _markers(trades),
        "params": params_data,
        "validation": validation,
    }
    return ExecutionBacktestResult(
        run_id=uuid.uuid4().hex[:12],
        params=params_data,
        equity_curve=equity_curve,
        trades=trades,
        positions=positions,
        stats=stats,
        trace=trace.to_dict(),
        validation=validation,
        visualization=visualization,
    )


def run_execution_grid(
    script_entrypoint: str | Path,
    dataset: PreparedExecutionDataset,
    execution_spec: ExecutionSpec | Mapping[str, Any] | None,
    param_grid: Mapping[str, list[Any]] | list[Mapping[str, Any]],
    *,
    workers: int = 1,
    parallel: str = "serial",
    rank_by: str = "net_return",
    top_n_artifacts: int = 10,
) -> ExecutionGridResult:
    params_list = _expand_grid(param_grid)
    grid_id = uuid.uuid4().hex[:12]
    if parallel == "serial" or workers <= 1:
        results = [
            simulate_execution(script_entrypoint, dataset, execution_spec, params)
            for params in params_list
        ]
    elif parallel == "thread":
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(
                executor.map(
                    lambda item: simulate_execution(
                        script_entrypoint, dataset, execution_spec, item
                    ),
                    params_list,
                )
            )
    elif parallel == "process":
        payload = dataset.to_dict()
        match execution_spec:
            case ExecutionSpec():
                spec_dict = execution_spec.to_dict()
            case _:
                spec_dict = dict(execution_spec or {})
        with ProcessPoolExecutor(max_workers=workers) as executor:
            results = list(
                executor.map(
                    _process_run,
                    [
                        (str(script_entrypoint), payload, spec_dict, dict(params))
                        for params in params_list
                    ],
                )
            )
    else:
        raise ValueError("parallel must be serial, thread, or process")

    run_rows = [_grid_row(result, rank_by=rank_by) for result in results]
    valid = [row for row in run_rows if row["validation"]["execution_valid"] is True]
    invalid = [
        row for row in run_rows if row["validation"]["execution_valid"] is not True
    ]
    ranked = sorted(valid, key=lambda row: float(row[rank_by] or 0), reverse=True)
    return ExecutionGridResult(
        grid_id=grid_id,
        rank_by=rank_by,
        runs=run_rows,
        ranked=ranked[:top_n_artifacts],
        invalid=invalid,
    )


def write_backtest_artifacts(
    result: ExecutionBacktestResult | ExecutionGridResult,
    output_dir: str | Path,
) -> dict[str, str]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    match result:
        case ExecutionGridResult():
            summary = root / "summary.json"
            runs = root / "runs.jsonl"
            summary.write_text(
                json.dumps(result.to_dict(), indent=2, default=str) + "\n",
                encoding="utf-8",
            )
            with runs.open("w", encoding="utf-8") as handle:
                for row in result.runs:
                    handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
            return {"summary": str(summary), "runs": str(runs)}
        case _:
            latest = root / "latest.json"
            visualization = root / "visualization.json"
            latest.write_text(
                json.dumps(result.to_dict(), indent=2, default=str) + "\n",
                encoding="utf-8",
            )
            visualization.write_text(
                json.dumps(result.visualization, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
            return {"latest": str(latest), "visualization": str(visualization)}


def _process_run(
    payload: tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]],
) -> ExecutionBacktestResult:
    script_entrypoint, dataset_payload, spec, params = payload
    dataset = PreparedExecutionDataset.from_rows(
        dataset_payload["bars"], dataset_payload.get("metadata")
    )
    return simulate_execution(script_entrypoint, dataset, spec, params)


def _load_strategy(
    script_entrypoint: str | Path | Callable[..., Any], params: dict[str, Any]
) -> Any:
    if callable(script_entrypoint):
        return script_entrypoint(params)
    path = Path(script_entrypoint)
    module_name = f"_wayfinder_execution_strategy_{abs(hash(str(path.resolve())))}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Cannot load strategy script: {path}")
    module = importlib.util.module_from_spec(spec)
    old_path = list(sys.path)
    try:
        sys.path.insert(0, str(path.parent))
        spec.loader.exec_module(module)
    finally:
        sys.path = old_path
    build_strategy = getattr(module, "build_strategy", None)
    if callable(build_strategy):
        return build_strategy(params)
    decide = getattr(module, "decide", None)
    if callable(decide):
        return decide
    raise ValueError(
        "Execution strategy must expose build_strategy(params) or decide(ctx)"
    )


def _call_decide(strategy: Any, ctx: ExecutionContext) -> list[Any]:
    decide = getattr(strategy, "decide", strategy)
    result = decide(ctx)
    if asyncio.iscoroutine(result):
        result = asyncio.run(result)
    match result:
        case None:
            return []
        case Mapping():
            return [result]
        case _:
            return list(result)


def _evaluate_brackets(
    *,
    ledger: PositionLedger,
    brackets: dict[str, dict[str, Any]],
    bars_by_symbol: Mapping[str, dict[str, Any]],
    broker: BacktestBroker,
    timestamp: str,
    trace: ExecutionTrace,
    trades: list[dict[str, Any]],
) -> None:
    for symbol, position in list(ledger.positions.items()):
        bracket = brackets.get(symbol)
        bar = bars_by_symbol.get(symbol)
        if not bracket or bar is None:
            continue
        result = BracketEngine.resolve_intrabar(
            bar,
            position.side,
            _float_or_none(bracket.get("stop_loss")),
            _float_or_none(bracket.get("take_profit")),
            str(bracket.get("policy") or "conservative"),
        )
        trace.bracket_events.append(
            {"timestamp": timestamp, "symbol": symbol, **result}
        )
        if not result["hit"] or result["price"] is None:
            continue
        side = "sell" if position.side == "long" else "buy"
        intent = OrderIntent(
            action=result["exit_type"],
            venue="backtest",
            symbol=symbol,
            side=side,
            size=position.size,
            reduce_only=True,
            metadata={"bracket": result, "position_side": position.side},
        )
        fill = broker.execute(intent, price=float(result["price"]), timestamp=timestamp)
        ledger.apply_fill(fill)
        trace.fills.append(fill.to_dict())
        if fill.successful:
            trades.append(fill.to_dict())
        brackets.pop(symbol, None)


def _bars_at_timestamp(view: CompletedBarsView, timestamp: Any) -> dict[str, Any]:
    bars: dict[str, Any] = {}
    for symbol in view.symbols:
        try:
            bars[symbol] = view.row_at(timestamp, symbol=symbol)
        except ValueError:
            continue
    return bars


def _mark_to_market(
    ledger: PositionLedger, close_by_symbol: Mapping[str, float]
) -> float:
    total = 0.0
    for position in ledger.positions.values():
        direction = 1 if position.side == "long" else -1
        close = float(close_by_symbol.get(position.symbol, position.avg_price))
        total += direction * (close - position.avg_price) * position.size
    return total


def _stats(
    equity_curve: list[dict[str, Any]], trades: list[dict[str, Any]]
) -> dict[str, Any]:
    if not equity_curve:
        return {"net_return": 0.0, "trade_count": 0}
    start = float(equity_curve[0]["equity"])
    end = float(equity_curve[-1]["equity"])
    return {
        "net_return": (end / start - 1.0) if start else 0.0,
        "ending_equity": end,
        "trade_count": len(trades),
    }


def _markers(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    markers: list[dict[str, Any]] = []
    for trade in trades:
        raw = trade["raw"]
        metadata = raw["intent_metadata"]
        action = str(raw["intent_action"] or "").upper()
        markers.append(
            {
                "timestamp": trade["timestamp"],
                "symbol": trade["symbol"],
                "side": metadata.get("position_side") or trade["side"],
                "price": trade["avg_price"],
                "kind": "exit"
                if trade["reduce_only"] or action in REDUCE_ONLY_ACTIONS
                else "entry",
                "label": action or ("exit" if trade["reduce_only"] else "entry"),
            }
        )
    return markers


def _expand_grid(
    param_grid: Mapping[str, list[Any]] | list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    match param_grid:
        case list():
            return [dict(item) for item in param_grid]
        case _:
            keys = list(param_grid.keys())
            values = [param_grid[key] for key in keys]
            return [
                dict(zip(keys, combo, strict=True))
                for combo in itertools.product(*values)
            ]


def _grid_row(result: ExecutionBacktestResult, *, rank_by: str) -> dict[str, Any]:
    return {
        "run_id": result.run_id,
        "params": result.params,
        "stats": result.stats,
        "validation": result.validation,
        rank_by: result.stats.get(rank_by),
    }


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
