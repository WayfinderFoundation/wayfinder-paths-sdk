from __future__ import annotations

import asyncio
import itertools
import json
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.execution.engine import (
    EngineState,
    LiquidationConfig,
    run_tick,
)
from wayfinder_paths.jobs.execution.primitives import (
    DEFAULT_INITIAL_CAPITAL,
    CompletedBarsView,
    ExecutionSpec,
    ExecutionTrace,
    FillEvent,
    OrderIntent,
    PositionLedger,
    StateSnapshot,
    TradeCapacity,
    _load_module_from_path,
    bar_interval_seconds,
)
from wayfinder_paths.jobs.execution.validation import validate_execution_trace
from wayfinder_paths.jobs.execution.venues import VenueCapabilities, VenueState

REDUCE_ONLY_ACTIONS = frozenset({"CLOSE", "STOP_LOSS", "TAKE_PROFIT"})


@dataclass
class PreparedExecutionDataset:
    bars: CompletedBarsView
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_rows(
        cls, rows: list[Mapping[str, Any]], metadata: Mapping[str, Any] | None = None
    ) -> PreparedExecutionDataset:
        return cls(
            CompletedBarsView.from_rows(rows), dict(metadata) if metadata else {}
        )

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
    # Additive: which optimizer produced this result ("grid" | "optuna") and
    # the search settings when not an exhaustive grid.
    optimizer: str = "grid"
    search: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BacktestBroker:
    capabilities = VenueCapabilities(
        market_kind="perp",
        supports_brackets=True,
        supports_shorts=True,
        supports_notional_sizing=True,
        supports_limit_orders=True,
    )

    def __init__(self, *, fee_bps: float = 0.0, slippage_bps: float = 0.0) -> None:
        self.fee_bps = fee_bps
        self.slippage_bps = slippage_bps

    async def place(
        self,
        intent: OrderIntent,
        *,
        timestamp: str,
        price: float | None = None,
    ) -> FillEvent:
        return self.execute(intent, price=float(price or 0.0), timestamp=timestamp)

    async def fetch_state(self, symbols: Any = ()) -> VenueState:
        return VenueState(source="backtest")

    async def get_capacity(self, symbol: str, side: str) -> TradeCapacity:
        return TradeCapacity(safe=True, source="backtest_fixture")

    async def cancel(self, client_order_id: str) -> FillEvent:
        return FillEvent(
            status="rejected",
            venue="backtest",
            symbol="",
            side="",
            error="cancel is not supported in backtest",
            client_order_id=client_order_id,
        )

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
    spec = ExecutionSpec.coerce(execution_spec)
    params_data = dict(params) if params else {}
    strategy = _load_strategy(script_entrypoint, params_data)
    broker = BacktestBroker(
        fee_bps=float(params_data.get("fee_bps") or 0.0),
        slippage_bps=float(params_data.get("slippage_bps") or 0.0),
    )
    state = EngineState()
    trace = ExecutionTrace(execution_spec=spec.to_dict())
    trades: list[dict[str, Any]] = []
    positions: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    price_series: dict[str, list[dict[str, Any]]] = {
        symbol: [] for symbol in dataset.bars.symbols
    }
    initial_capital = float(
        params_data.get("initial_capital") or DEFAULT_INITIAL_CAPITAL
    )
    capacity = TradeCapacity(
        max_notional=float(params_data.get("max_notional") or 1_000_000.0),
        available_margin=float(params_data.get("available_margin") or 1_000_000.0),
        max_position_size=None,
        safe=True,
        source="backtest_fixture",
    )
    # None unless params["enable_liquidation"] is truthy — default-off parity.
    liquidation = LiquidationConfig.from_params(params_data)
    # When declared, each tick sees the same bounded trailing window the live
    # driver fetches (lookback_bars) instead of full history — this is a
    # live-parity choice for path-dependent indicators AND turns per-tick
    # strategy recompute from O(n) into O(k). Unset keeps full history.
    raw_lookback = params_data.get("lookback_bars")
    lookback_bars = int(raw_lookback) if raw_lookback else None

    async def _run_simulation() -> None:
        for index, timestamp in enumerate(dataset.bars.timestamps):
            bars_by_symbol = _bars_at_timestamp(dataset.bars, timestamp)
            if not bars_by_symbol:
                continue
            for symbol, bar in bars_by_symbol.items():
                price_series[symbol].append(
                    {
                        "timestamp": timestamp.isoformat(),
                        "value": bar.close,
                        "open": bar.open,
                        "high": bar.high,
                        "low": bar.low,
                        "close": bar.close,
                        "volume": bar.volume,
                    }
                )
            tick = await run_tick(
                strategy,
                view=(
                    dataset.bars.window(index, lookback_bars)
                    if lookback_bars
                    else dataset.bars.through(index)
                ),
                brokers={"*": broker},
                state=state,
                spec=spec,
                params=params_data,
                timestamp=timestamp,
                snapshot=StateSnapshot(status="valid"),
                capacity=capacity,
                trace=trace,
                liquidation=liquidation,
            )
            if tick.skipped:
                continue
            trades.extend(tick.trade_rows)
            positions.append({"timestamp": timestamp.isoformat(), **tick.ledger_snapshot})
            mark_to_market = _mark_to_market(
                state.ledger,
                {symbol: bar.close for symbol, bar in bars_by_symbol.items()},
            )
            equity = initial_capital + state.ledger.realized_pnl + mark_to_market
            equity_curve.append(
                {
                    "timestamp": timestamp.isoformat(),
                    "equity": equity,
                    "value": equity,
                    "realized_pnl": state.ledger.realized_pnl,
                    "unrealized_pnl": mark_to_market,
                }
            )

    _run_sync(_run_simulation())

    validation = validate_execution_trace(trace.to_dict(), spec)
    stats = _stats(
        equity_curve,
        trades,
        positions,
        bar_interval_seconds(spec.data_contract.get("bar_interval")),
        params=params_data,
        guard_events=trace.guard_events,
        price_series=price_series,
    )
    drawdown_curve = _drawdown_curve(equity_curve)
    visualization = {
        "schema_version": "1.0",
        "market_kind": spec.market_kind,
        "view_type": spec.view_type,
        "symbols": dataset.bars.symbols,
        "series": [
            {"name": "equity", "kind": "equity_curve", "points": equity_curve},
            {"name": "drawdown", "kind": "drawdown_curve", "points": drawdown_curve},
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


GRID_RANK_KEYS = frozenset(
    {
        "net_return",
        "ending_equity",
        "trade_count",
        "sharpe",
        "max_drawdown_pct",
        "win_rate",
        "profit_factor",
        "avg_trade_pnl",
        "exposure_pct",
        "return_on_margin",
        "sortino",
        "calmar",
        "cagr",
    }
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
    if rank_by not in GRID_RANK_KEYS:
        raise ValueError(
            f"rank_by must be one of {sorted(GRID_RANK_KEYS)}, got {rank_by!r}"
        )
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
        spec_dict = ExecutionSpec.coerce(execution_spec).to_dict()
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
    *,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    stamp = dict(extra) if extra else {}
    match result:
        case ExecutionGridResult():
            summary = root / "summary.json"
            runs = root / "runs.jsonl"
            summary.write_text(
                json.dumps({**result.to_dict(), **stamp}, indent=2, default=str)
                + "\n",
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
                json.dumps({**result.to_dict(), **stamp}, indent=2, default=str)
                + "\n",
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
    module = _load_module_from_path(Path(script_entrypoint))
    build_strategy = getattr(module, "build_strategy", None)
    if callable(build_strategy):
        return build_strategy(params)
    decide = getattr(module, "decide", None)
    if callable(decide):
        return decide
    raise ValueError(
        "Execution strategy must expose build_strategy(params) or decide(ctx)"
    )


def _run_sync(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    coro.close()
    raise RuntimeError(
        "simulate_execution cannot be called from a running event loop; "
        "call it from sync code or a worker thread"
    )


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


SECONDS_PER_YEAR = 365 * 24 * 3600


def _stats(
    equity_curve: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    positions: list[dict[str, Any]] | None = None,
    bar_seconds: int | None = None,
    params: Mapping[str, Any] | None = None,
    *,
    guard_events: list[dict[str, Any]] | None = None,
    price_series: Mapping[str, list[Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
    leverage = max(float((params or {}).get("leverage") or 1.0), 1e-9)
    peak_notional = _peak_notional(positions)
    margin_used = peak_notional / leverage if peak_notional else 0.0
    exit_pnls = [
        float(trade["realized_pnl_delta"])
        for trade in trades
        if trade.get("realized_pnl_delta") is not None
        and (trade.get("reduce_only") or trade.get("realized_pnl_delta"))
    ]
    trade_stats = _per_trade_stats(exit_pnls)
    durations = _trade_durations(trades)
    total_turnover = sum(
        abs(float(trade.get("filled_size") or 0.0))
        * float(trade.get("avg_price") or 0.0)
        for trade in trades
    )
    liquidations = [
        event
        for event in guard_events or []
        if event.get("kind") == "liquidation"
    ]
    common = {
        "buy_hold_return": _buy_hold_return(price_series),
        "total_fees": sum(float(trade.get("fee") or 0.0) for trade in trades),
        "total_funding": sum(
            float(event.get("amount") or 0.0)
            for event in guard_events or []
            if event.get("kind") == "funding_applied"
        ),
        "total_turnover_usd": total_turnover,
        "liquidation_count": len(liquidations),
        "liquidated_at": liquidations[0].get("timestamp") if liquidations else None,
        **trade_stats,
        **durations,
    }
    if not equity_curve:
        return {
            "net_return": 0.0,
            "ending_equity": 0.0,
            "trade_count": len(trades),
            "sharpe": None,
            "max_drawdown_pct": 0.0,
            "win_rate": None,
            "profit_factor": None,
            "avg_trade_pnl": None,
            "exposure_pct": 0.0,
            "peak_notional_usd": peak_notional,
            "margin_used": margin_used,
            "return_on_margin": None,
            "sortino": None,
            "volatility_ann": None,
            "cagr": 0.0,
            "return_ann": 0.0,
            "calmar": 0.0,
            "max_drawdown_duration_s": 0.0,
            "avg_drawdown_duration_s": 0.0,
            "avg_drawdown": 0.0,
            "avg_turnover": 0.0,
            **common,
        }
    start = equity_curve[0]["equity"]
    end = equity_curve[-1]["equity"]
    drawdowns = _drawdown_curve(equity_curve)
    wins = [pnl for pnl in exit_pnls if pnl > 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(pnl for pnl in exit_pnls if pnl < 0))
    exposed = (
        sum(1 for row in positions if row.get("positions")) if positions else 0
    )
    if bar_seconds is None:
        bar_seconds = _inferred_bar_seconds(equity_curve)
    periods_per_year = SECONDS_PER_YEAR / bar_seconds if bar_seconds else None
    returns = _equity_returns(equity_curve)
    max_drawdown_pct = min(
        (point["drawdown_pct"] for point in drawdowns), default=0.0
    )
    cagr = _cagr(start, end, len(returns), periods_per_year)
    dd_durations = _drawdown_durations(drawdowns)
    negative_dds = [
        point["drawdown_pct"] for point in drawdowns if point["drawdown_pct"] < 0
    ]
    return {
        "net_return": (end / start - 1.0) if start else 0.0,
        "ending_equity": end,
        "trade_count": len(trades),
        "sharpe": _sharpe(equity_curve, bar_seconds),
        "max_drawdown_pct": max_drawdown_pct,
        "win_rate": (len(wins) / len(exit_pnls)) if exit_pnls else None,
        "profit_factor": (gross_profit / gross_loss) if gross_loss else None,
        "avg_trade_pnl": (sum(exit_pnls) / len(exit_pnls)) if exit_pnls else None,
        "exposure_pct": (exposed / len(positions)) if positions else 0.0,
        # Entry-price basis (deterministic from ledger snapshots, not marked).
        "peak_notional_usd": peak_notional,
        "margin_used": margin_used,
        "return_on_margin": ((end - start) / margin_used) if margin_used else None,
        # Legacy-parity risk metrics (formulas from core/backtesting/stats.py;
        # sortino/volatility use ddof=0 there — deliberately different from the
        # ddof=1 in the preexisting `sharpe`, which must not change).
        "sortino": _sortino(returns, periods_per_year),
        "volatility_ann": _volatility_ann(returns, periods_per_year),
        "cagr": cagr,
        "return_ann": cagr,
        "calmar": abs(cagr / max_drawdown_pct) if max_drawdown_pct else 0.0,
        "max_drawdown_duration_s": dd_durations["max"],
        "avg_drawdown_duration_s": dd_durations["avg"],
        "avg_drawdown": (
            sum(negative_dds) / len(negative_dds) if negative_dds else 0.0
        ),
        "avg_turnover": _avg_turnover(equity_curve, trades),
        **common,
    }


def _equity_returns(equity_curve: list[dict[str, Any]]) -> list[float]:
    values = [float(row["equity"]) for row in equity_curve]
    return [
        (curr / prev - 1.0) if prev else 0.0
        for prev, curr in zip(values, values[1:], strict=False)
    ]


def _sortino(
    returns: list[float], periods_per_year: float | None
) -> float | None:
    if len(returns) < 2 or not periods_per_year:
        return None
    mean = sum(returns) / len(returns)
    downside = [r for r in returns if r < 0]
    if not downside:
        return 0.0  # legacy convention: no downside vol reports 0, not inf
    downside_mean = sum(downside) / len(downside)
    downside_vol = (
        sum((r - downside_mean) ** 2 for r in downside) / len(downside)
    ) ** 0.5
    if downside_vol == 0:
        return 0.0
    return (periods_per_year**0.5) * mean / downside_vol


def _volatility_ann(
    returns: list[float], periods_per_year: float | None
) -> float | None:
    if len(returns) < 2 or not periods_per_year:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / len(returns)  # ddof=0
    return (variance**0.5) * (periods_per_year**0.5)


def _cagr(
    start: float,
    end: float,
    period_count: int,
    periods_per_year: float | None,
) -> float:
    if not periods_per_year or period_count <= 0 or start <= 0:
        return 0.0
    years = period_count / periods_per_year
    if years <= 0 or end < 0:
        return 0.0
    return float((end / start) ** (1 / years) - 1)


def _per_trade_stats(exit_pnls: list[float]) -> dict[str, Any]:
    """SQN / Kelly / best / worst over discrete per-trade PnLs.

    Legacy computes these on per-period returns (weights paradigm); jobs has
    discrete round-trip PnLs, so the same formulas run on `exit_pnls`.
    """
    if not exit_pnls:
        return {
            "sqn": None,
            "kelly_criterion": None,
            "best_trade_pnl": None,
            "worst_trade_pnl": None,
        }
    mean = sum(exit_pnls) / len(exit_pnls)
    if len(exit_pnls) > 1:
        variance = sum((p - mean) ** 2 for p in exit_pnls) / (len(exit_pnls) - 1)
        std = variance**0.5
    else:
        std = 0.0
    sqn = (len(exit_pnls) ** 0.5) * mean / std if std > 0 else 0.0
    wins = [p for p in exit_pnls if p > 0]
    losses = [p for p in exit_pnls if p < 0]
    if wins and losses:
        win_rate = len(wins) / len(exit_pnls)
        avg_win = sum(wins) / len(wins)
        avg_loss = abs(sum(losses) / len(losses))
        win_loss_ratio = avg_win / avg_loss if avg_loss > 0 else 0.0
        kelly = (
            win_rate - ((1 - win_rate) / win_loss_ratio)
            if win_loss_ratio > 0
            else 0.0
        )
    else:
        kelly = 0.0
    return {
        "sqn": sqn,
        "kelly_criterion": kelly,
        "best_trade_pnl": max(exit_pnls),
        "worst_trade_pnl": min(exit_pnls),
    }


def _trade_durations(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Open→close durations per symbol, in seconds. Each reduce-only fill
    emits (close_ts − entry_ts) against the entry that opened the current
    position; the entry timestamp clears once the position is fully closed."""
    entry_ts: dict[str, pd.Timestamp] = {}
    remaining: dict[str, float] = {}
    spans: list[float] = []
    for trade in trades:
        symbol = str(trade.get("symbol") or "")
        raw_ts = trade.get("timestamp")
        if not symbol or raw_ts is None:
            continue
        ts = pd.Timestamp(raw_ts)
        size = abs(float(trade.get("filled_size") or 0.0))
        if trade.get("reduce_only"):
            opened = entry_ts.get(symbol)
            if opened is not None:
                spans.append(float((ts - opened).total_seconds()))
            remaining[symbol] = remaining.get(symbol, 0.0) - size
            if remaining.get(symbol, 0.0) <= 1e-12:
                entry_ts.pop(symbol, None)
                remaining.pop(symbol, None)
        else:
            if remaining.get(symbol, 0.0) <= 0.0:
                entry_ts[symbol] = ts
            remaining[symbol] = remaining.get(symbol, 0.0) + size
    if not spans:
        return {"max_trade_duration_s": None, "avg_trade_duration_s": None}
    return {
        "max_trade_duration_s": max(spans),
        "avg_trade_duration_s": sum(spans) / len(spans),
    }


def _drawdown_durations(drawdowns: list[dict[str, Any]]) -> dict[str, float]:
    """Contiguous drawdown_pct<0 runs; a run ends at the recovery point (the
    first bar back at/above the peak), or at the last bar if never recovered."""
    periods: list[float] = []
    start_ts: pd.Timestamp | None = None
    last_ts: pd.Timestamp | None = None
    for point in drawdowns:
        ts = pd.Timestamp(point["timestamp"])
        last_ts = ts
        in_drawdown = point["drawdown_pct"] < 0
        if in_drawdown and start_ts is None:
            start_ts = ts
        elif not in_drawdown and start_ts is not None:
            periods.append(float((ts - start_ts).total_seconds()))
            start_ts = None
    if start_ts is not None and last_ts is not None:
        periods.append(float((last_ts - start_ts).total_seconds()))
    if not periods:
        return {"max": 0.0, "avg": 0.0}
    return {"max": max(periods), "avg": sum(periods) / len(periods)}


def _buy_hold_return(
    price_series: Mapping[str, list[Mapping[str, Any]]] | None,
) -> float | None:
    """Equal-weight buy & hold over all symbols (legacy convention)."""
    if not price_series:
        return None
    asset_returns: list[float] = []
    for points in price_series.values():
        closes = [
            float(point["close"])
            for point in points
            if point.get("close") is not None
        ]
        if len(closes) >= 2 and closes[0]:
            asset_returns.append(closes[-1] / closes[0] - 1.0)
    if not asset_returns:
        return None
    return sum(asset_returns) / len(asset_returns)


def _avg_turnover(
    equity_curve: list[dict[str, Any]], trades: list[dict[str, Any]]
) -> float:
    """Mean per-bar traded notional over equity (0 for tradeless bars)."""
    if not equity_curve:
        return 0.0
    notional_by_ts: dict[str, float] = {}
    for trade in trades:
        ts = str(trade.get("timestamp"))
        notional_by_ts[ts] = notional_by_ts.get(ts, 0.0) + abs(
            float(trade.get("filled_size") or 0.0)
        ) * float(trade.get("avg_price") or 0.0)
    ratios = [
        (notional_by_ts.get(str(row["timestamp"]), 0.0) / float(row["equity"]))
        if float(row["equity"])
        else 0.0
        for row in equity_curve
    ]
    return sum(ratios) / len(ratios)


def _peak_notional(positions: list[dict[str, Any]] | None) -> float:
    peak = 0.0
    for row in positions or []:
        total = sum(
            float(record.get("size") or 0.0) * float(record.get("avg_price") or 0.0)
            for record in (row.get("positions") or {}).values()
        )
        peak = max(peak, total)
    return peak


def _sharpe(
    equity_curve: list[dict[str, Any]], bar_seconds: int | None
) -> float | None:
    values = [float(row["equity"]) for row in equity_curve]
    if len(values) < 3:
        return None
    returns = [
        (curr / prev - 1.0) if prev else 0.0
        for prev, curr in zip(values, values[1:], strict=False)
    ]
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = variance**0.5
    if std == 0:
        return None
    if bar_seconds is None:
        bar_seconds = _inferred_bar_seconds(equity_curve)
    if not bar_seconds:
        return None
    periods_per_year = SECONDS_PER_YEAR / bar_seconds
    return (mean / std) * (periods_per_year**0.5)


def _inferred_bar_seconds(equity_curve: list[dict[str, Any]]) -> int | None:
    timestamps = pd.to_datetime(
        [row["timestamp"] for row in equity_curve], utc=True
    )
    if len(timestamps) < 2:
        return None
    deltas = timestamps.to_series().diff().dropna().dt.total_seconds()
    if deltas.empty:
        return None
    median = float(deltas.median())
    return int(median) if median > 0 else None


def _drawdown_curve(equity_curve: list[dict[str, Any]]) -> list[dict[str, Any]]:
    peak: float | None = None
    points: list[dict[str, Any]] = []
    for row in equity_curve:
        equity = float(row.get("equity") or row.get("value") or 0)
        peak = equity if peak is None else max(peak, equity)
        drawdown = equity - peak
        drawdown_pct = drawdown / peak if peak else 0.0
        points.append(
            {
                "timestamp": row.get("timestamp"),
                "value": drawdown_pct,
                "drawdown": drawdown,
                "drawdown_pct": drawdown_pct,
                "equity": equity,
                "peak_equity": peak,
            }
        )
    return points


def _markers(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    markers: list[dict[str, Any]] = []
    for trade in trades:
        raw = trade["raw"]
        metadata = raw["intent_metadata"]
        action = raw["intent_action"].upper()
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
