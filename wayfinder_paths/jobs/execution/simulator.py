from __future__ import annotations

import asyncio
import itertools
import json
import os
import sys
import time
import uuid
from collections import defaultdict
from collections.abc import Callable, Mapping
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.execution.engine import (
    EngineState,
    LiquidationConfig,
    _bars_at_timestamp,
    run_tick,
)
from wayfinder_paths.jobs.execution.features import apply_precompute
from wayfinder_paths.jobs.execution.primitives import (
    DEFAULT_INITIAL_CAPITAL,
    REDUCE_ONLY_ACTIONS,
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
    # Run telemetry: wall time, bars/sec, per-bar tick timing, the compute
    # window used, and a self-diagnostic `hint` when the run looks O(N²).
    # Additive (default {}) so older callers/readers are unaffected.
    profile: dict[str, Any] = field(default_factory=dict)

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
    # Additive: per-factor marginal effects + interaction checks over a
    # factorial grid (grid_factor_attribution) — the ablation summary a
    # compound proposal must cite.
    factor_attribution: dict[str, Any] | None = None
    # Additive: neighbor-robustness of the top cell (grid_plateau) — only for
    # exhaustive dict-of-lists grids; None for optuna and list-of-dicts.
    plateau: dict[str, Any] | None = None

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

    def __init__(
        self,
        *,
        fee_bps: float = 0.0,
        maker_fee_bps: float = 0.0,
        slippage_bps: float = 0.0,
    ) -> None:
        self.fee_bps = fee_bps
        self.maker_fee_bps = maker_fee_bps
        self.slippage_bps = slippage_bps

    async def place(
        self,
        intent: OrderIntent,
        *,
        timestamp: str,
        price: float | None = None,
    ) -> FillEvent:
        if intent.limit_price is not None and not intent.metadata.get("_resting_fill"):
            return FillEvent(
                status="resting",
                venue=intent.venue,
                symbol=intent.symbol,
                side=intent.side,
                order_id=intent.client_order_id,
                client_order_id=intent.client_order_id,
                reduce_only=intent.reduce_only,
                raw={"intent_action": intent.action, "liquidity": "maker"},
                timestamp=timestamp,
            )
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
        maker_fill = bool(
            intent.limit_price is not None and intent.metadata.get("_resting_fill")
        )
        side_multiplier = 1 if str(intent.side).lower() in {"buy", "long"} else -1
        fill_price = (
            price
            if maker_fill
            else price * (1 + side_multiplier * self.slippage_bps / 10_000)
        )
        fee_bps = self.maker_fee_bps if maker_fill else self.fee_bps
        fee = abs(size * fill_price) * fee_bps / 10_000
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
                "liquidity": "maker" if maker_fill else "taker",
            },
            timestamp=timestamp,
        )


# Default per-side taker fee (bps) applied when a strategy does not declare its
# own `fee_bps`. Backtests used to default to zero, which flattered every
# strategy — and disproportionately the small-edge Hyperliquid scalpers whose
# per-trade edge is smaller than real fees. Hyperliquid base taker is 4.5 bps
# (HIP-3 / builder-deployed markets can be higher, so this is a floor).
# Strategies override with params["fee_bps"] (e.g. 0.0 for a maker-only book).
_DEFAULT_TAKER_FEE_BPS: dict[str, float] = {"hyperliquid": 4.5, "hl": 4.5}
_DEFAULT_MAKER_FEE_BPS: dict[str, float] = {"hyperliquid": 1.5, "hl": 1.5}


def _strategy_venue(strategy: Any) -> str:
    """Venue a strategy declares on itself (e.g. ShortMomentumStrategy.params),
    used for the fee default when the caller didn't pass `venue` in params."""
    strat_params = getattr(strategy, "params", None)
    if isinstance(strat_params, Mapping):
        return str(strat_params.get("venue") or "")
    return ""


def _resolve_fee_bps(params_data: Mapping[str, Any], strategy: Any = None) -> float:
    explicit = params_data.get("fee_bps")
    if explicit is not None:
        return float(explicit)
    venue = (
        str(params_data.get("venue") or _strategy_venue(strategy) or "").strip().lower()
    )
    return _DEFAULT_TAKER_FEE_BPS.get(venue, 0.0)


def _resolve_maker_fee_bps(
    params_data: Mapping[str, Any], strategy: Any = None
) -> float:
    explicit = params_data.get("maker_fee_bps")
    if explicit is not None:
        return float(explicit)
    venue = (
        str(params_data.get("venue") or _strategy_venue(strategy) or "").strip().lower()
    )
    return _DEFAULT_MAKER_FEE_BPS.get(venue, 0.0)


# Default per-bar compute window. Bounding the view the simulator hands each
# tick keeps the DEFAULT backtest O(N·k) instead of O(N²): a strategy that
# recomputes indicators over the whole handed frame goes quadratic when that
# frame grows with the replay index (the classic "simple backtest pegs the
# CPU" trap). 512 bars covers the lookback of essentially every standard
# indicator (SMA200, ATR/ADX, long EMAs) with margin. Strategies tune it via
# `warmup_bars`; genuine since-genesis strategies opt out with
# `full_history: true`.
DEFAULT_WARMUP_BARS = 512


def _resolve_compute_window(
    params_data: Mapping[str, Any], strategy: Any
) -> tuple[int | None, str, bool]:
    """Size of the trailing view handed to `decide()` each bar.

    Resolution (first hit wins):
      1. ``params['warmup_bars']``  — explicit, canonical name.
      2. ``params['lookback_bars']`` — back-compat with the old windowing lever.
      3. ``strategy.warmup_bars``   — strategy-declared attribute.
      4. ``DEFAULT_WARMUP_BARS``.
    ``params['full_history']`` truthy opts back into full-history views.

    Returns ``(window_size | None, source, full_history)``; ``None`` window ⇒
    full history (``through(index)``).
    """
    if params_data.get("full_history"):
        return None, "full_history", True
    for key in ("warmup_bars", "lookback_bars"):
        raw = params_data.get(key)
        if raw:
            return max(int(raw), 1), key, False
    attr = getattr(strategy, "warmup_bars", None)
    if attr:
        return max(int(attr), 1), "strategy.warmup_bars", False
    return DEFAULT_WARMUP_BARS, "default", False


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (pct / 100.0) * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)


def _tick_time_growing(tick_ms: list[float]) -> bool:
    """True when per-bar time trends up with the replay index — the fingerprint
    of an O(history) recompute inside `decide()`. Compares the mean of the
    first decile against the last; needs enough bars to be meaningful."""
    if len(tick_ms) < 40:
        return False
    decile = max(1, len(tick_ms) // 10)
    first = sum(tick_ms[:decile]) / decile
    last = sum(tick_ms[-decile:]) / decile
    return last > 5.0 and last > first * 3.0


def _fmt_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{seconds:.0f}s"
    return f"{seconds / 60:.1f}m"


def _emit_progress(
    done: int, total: int, wall_start: float, tick_ms: list[float]
) -> None:
    elapsed = time.perf_counter() - wall_start
    rate = done / elapsed if elapsed > 0 else 0.0
    eta = (total - done) / rate if rate > 0 else 0.0
    p95 = _percentile(tick_ms, 95)
    grow = " ↑growing" if _tick_time_growing(tick_ms) else ""
    print(
        f"[backtest] bar {done}/{total} · {rate:.0f} bars/s · "
        f"ETA {_fmt_duration(eta)} · tick p95 {p95:.0f}ms{grow}",
        file=sys.stderr,
        flush=True,
    )


def _build_profile(
    *,
    tick_ms: list[float],
    wall_start: float,
    total_bars: int,
    window_size: int | None,
    window_source: str,
    full_history: bool,
) -> dict[str, Any]:
    wall_s = time.perf_counter() - wall_start
    timed = len(tick_ms)
    mean_ms = sum(tick_ms) / timed if timed else 0.0
    growing = _tick_time_growing(tick_ms)
    profile: dict[str, Any] = {
        "wall_seconds": round(wall_s, 3),
        "bars_total": total_bars,
        "bars_timed": timed,
        "bars_per_second": round(timed / wall_s, 1) if wall_s > 0 else None,
        "tick_ms": {
            "mean": round(mean_ms, 2),
            "p50": round(_percentile(tick_ms, 50), 2),
            "p95": round(_percentile(tick_ms, 95), 2),
            "max": round(max(tick_ms), 2) if tick_ms else 0.0,
            "last": round(tick_ms[-1], 2) if tick_ms else 0.0,
        },
        "compute_window": "full_history" if full_history else window_size,
        "compute_window_source": window_source,
        "tick_time_growing": growing,
    }
    # Most "why is this backtest so slow" cases are a heavy full recompute in
    # decide() run once per bar × many bars on a small/throttled box — O(N)
    # with a big constant, not necessarily O(N²). The projection (measured per
    # bar × total bars) is machine-relative, so the hint fires exactly when the
    # run is actually slow on THIS box, and stays quiet when it's fine.
    if growing:
        profile["hint"] = (
            "Per-bar time is growing with the replay index — decide() is "
            "recomputing over an ever-larger frame (heading toward O(N²)). "
            "Compute on a bounded trailing window: set `warmup_bars` to your "
            "longest indicator lookback + a small buffer, or slice "
            "`ctx.view.window(...)` instead of the full `symbol_frame()`."
        )
    elif mean_ms >= 15.0:
        profile["hint"] = (
            f"Heavy per-bar work: ~{mean_ms:.0f} ms/bar × {total_bars} bars ≈ "
            f"{_fmt_duration(mean_ms * total_bars / 1000.0)} for the full run. "
            "decide() recomputes its indicators from scratch every bar — "
            "compute them incrementally or on a bounded window (`warmup_bars` / "
            "`ctx.view.window`), and iterate on a shorter backtest "
            "(`--quick`) before running the full history."
        )
    return profile


def simulate_execution(
    script_entrypoint: str | Path | Callable[..., Any],
    dataset: PreparedExecutionDataset,
    execution_spec: ExecutionSpec | Mapping[str, Any] | None = None,
    params: Mapping[str, Any] | None = None,
) -> ExecutionBacktestResult:
    spec = ExecutionSpec.coerce(execution_spec)
    params_data = dict(params) if params else {}
    strategy = _load_strategy(script_entrypoint, params_data)
    # One vectorized pass for strategy-declared derived columns (optional
    # `precompute` hook — see features.apply_precompute). Runs on the (already
    # quick_bars-truncated, feature-merged) dataset, so the replay's per-bar
    # decide() just reads columns instead of re-deriving indicators.
    dataset = PreparedExecutionDataset(
        apply_precompute(strategy, dataset.bars), dict(dataset.metadata)
    )
    broker = BacktestBroker(
        fee_bps=_resolve_fee_bps(params_data, strategy),
        maker_fee_bps=_resolve_maker_fee_bps(params_data, strategy),
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
    # Each tick sees a bounded trailing window (the same the live driver
    # fetches) so per-tick strategy recompute stays O(k), not O(index). This
    # is now the DEFAULT — full history is opt-in via `full_history: true`.
    window_size, window_source, full_history = _resolve_compute_window(
        params_data, strategy
    )

    total_bars = len(dataset.bars.timestamps)
    progress_every = max(1, total_bars // 20)
    tick_ms: list[float] = []
    wall_start = time.perf_counter()

    # Running last-known close per symbol so open positions are marked at their
    # most recent price, not just symbols that printed a bar THIS timestamp. In
    # a multi-symbol book where symbols do not all print every bar, marking an
    # absent symbol at avg_price (zero unrealized) makes equity oscillate as
    # symbols appear/disappear — corrupting net_return/drawdown/sharpe. Matches
    # mark_to_market_equity() (what decide() sees), which already uses the view's
    # latest close.
    last_close_by_symbol: dict[str, float] = {}

    async def _run_simulation() -> None:
        for index, timestamp in enumerate(dataset.bars.timestamps):
            bars_by_symbol = _bars_at_timestamp(dataset.bars, timestamp)
            if not bars_by_symbol:
                continue
            last_close_by_symbol.update(
                {symbol: bar.close for symbol, bar in bars_by_symbol.items()}
            )
            timestamp_iso = timestamp.isoformat()
            for symbol, bar in bars_by_symbol.items():
                price_series[symbol].append(
                    {
                        "timestamp": timestamp_iso,
                        "value": bar.close,
                        "open": bar.open,
                        "high": bar.high,
                        "low": bar.low,
                        "close": bar.close,
                        "volume": bar.volume,
                    }
                )
            tick_start = time.perf_counter()
            tick = await run_tick(
                strategy,
                view=(
                    dataset.bars.through(index)
                    if full_history
                    else dataset.bars.window(index, window_size)
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
            tick_ms.append((time.perf_counter() - tick_start) * 1000.0)
            if total_bars and (index + 1) % progress_every == 0:
                _emit_progress(index + 1, total_bars, wall_start, tick_ms)
            if tick.skipped:
                continue
            trades.extend(tick.trade_rows)
            positions.append(
                {"timestamp": timestamp.isoformat(), **tick.ledger_snapshot}
            )
            mark_to_market = _mark_to_market(state.ledger, last_close_by_symbol)
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

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "simulate_execution cannot be called from a running event loop; "
            "call it from sync code or a worker thread"
        )
    asyncio.run(_run_simulation())

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
    profile = _build_profile(
        tick_ms=tick_ms,
        wall_start=wall_start,
        total_bars=total_bars,
        window_size=window_size,
        window_source=window_source,
        full_history=full_history,
    )
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
        profile=profile,
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


def check_rank_key(rank_by: str) -> None:
    if rank_by not in GRID_RANK_KEYS:
        raise ValueError(
            f"rank_by must be one of {sorted(GRID_RANK_KEYS)}, got {rank_by!r}"
        )


def rank_and_partition(
    run_rows: list[dict[str, Any]], *, rank_by: str, top_n: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(ranked top-N valid rows, invalid rows) — the shared grid/optuna tail."""
    valid = [row for row in run_rows if row["validation"]["execution_valid"]]
    invalid = [row for row in run_rows if not row["validation"]["execution_valid"]]
    ranked = sorted(valid, key=lambda row: float(row[rank_by] or 0), reverse=True)
    return ranked[:top_n], invalid


def grid_plateau(
    run_rows: list[dict[str, Any]],
    param_grid: Mapping[str, list[Any]],
    *,
    rank_by: str,
) -> dict[str, Any] | None:
    """Neighbor-robustness of the top grid cell: neighbors differ in exactly
    one parameter by one step along that parameter's grid list, and
    plateau_score = mean(neighbor metric) / top metric. Near 1.0 means the
    optimum sits on a plateau; < 0.5 means a lone spike that is likely noise —
    the walk_forward docstring's failure mode (a top cell whose neighbors all
    lose), now measured instead of discovered out-of-sample. Returns None when
    nothing is swept (no axis with >1 value), the top metric is <= 0 (a ratio
    against a loss is meaningless), or the top cell has no valid neighbors."""
    valid = [row for row in run_rows if row["validation"]["execution_valid"]]
    axes = {key: list(values) for key, values in param_grid.items() if len(values) > 1}
    if not valid or not axes:
        return None
    top = max(valid, key=lambda row: float(row[rank_by] or 0))
    top_metric = float(top[rank_by] or 0)
    if top_metric <= 0:
        return None

    def is_neighbor(row: dict[str, Any]) -> bool:
        diffs = [
            key
            for key in param_grid
            if row["params"].get(key) != top["params"].get(key)
        ]
        if len(diffs) != 1 or diffs[0] not in axes:
            return False
        axis = axes[diffs[0]]
        try:
            step = abs(
                axis.index(row["params"][diffs[0]])
                - axis.index(top["params"][diffs[0]])
            )
        except ValueError:
            return False
        return step == 1

    neighbors = [row for row in valid if is_neighbor(row)]
    if not neighbors:
        return None
    neighbor_mean = sum(float(row[rank_by] or 0) for row in neighbors) / len(neighbors)
    score = neighbor_mean / top_metric
    result: dict[str, Any] = {
        "rank_by": rank_by,
        "top_params": top["params"],
        "top_metric": round(top_metric, 6),
        "neighbor_count": len(neighbors),
        "neighbor_mean": round(neighbor_mean, 6),
        "plateau_score": round(score, 3),
    }
    if score < 0.5:
        result["note"] = (
            f"top cell is a lone spike — its one-step neighbors average "
            f"{score:.0%} of its {rank_by}, which is likely noise. Prefer a "
            "parameter region where the neighbors also perform (the best "
            "plateau), and confirm with walk-forward before trusting it."
        )
    return result


def grid_factor_attribution(
    run_rows: list[dict[str, Any]],
    param_grid: Mapping[str, list[Any]],
    *,
    rank_by: str,
) -> dict[str, Any] | None:
    """Per-factor marginal effects over a factorial grid — the ablation
    summary a compound proposal must cite.

    Treats every swept axis as a factor: per-level means of the rank_by
    metric across ALL cells, the marginal effect for 2-level factors
    (mean(on) - mean(off)), and a pairwise interaction check for 2-level
    factors (does A's effect flip sign conditional on B's level). This is
    how "the improvement is mostly the exit change; the volume gate only
    helps when the MTF filter is on" becomes a stated, checkable claim
    instead of a guess about the winning cell."""
    valid = [row for row in run_rows if row["validation"]["execution_valid"]]
    axes = {key: list(values) for key, values in param_grid.items() if len(values) > 1}
    if not valid or not axes:
        return None

    def metric(row: dict[str, Any]) -> float:
        return float(row.get(rank_by) or 0.0)

    def mean_where(predicate: Any) -> float | None:
        rows = [metric(r) for r in valid if predicate(r)]
        return round(sum(rows) / len(rows), 6) if rows else None

    factors: dict[str, Any] = {}
    for axis, levels in axes.items():
        level_means = {
            str(level): mean_where(lambda r, a=axis, v=level: r["params"].get(a) == v)
            for level in levels
        }
        entry: dict[str, Any] = {"levels": level_means}
        if len(levels) == 2:
            low, high = level_means[str(levels[0])], level_means[str(levels[1])]
            if low is not None and high is not None:
                entry["marginal_effect"] = round(high - low, 6)
        factors[axis] = entry

    interactions: list[dict[str, Any]] = []
    two_level = [axis for axis, levels in axes.items() if len(levels) == 2]
    for i, a in enumerate(two_level):
        for b in two_level[i + 1 :]:
            effects = []
            for b_level in axes[b]:
                on = mean_where(
                    lambda r, a=a, b=b, bl=b_level: r["params"].get(a) == axes[a][1]
                    and r["params"].get(b) == bl
                )
                off = mean_where(
                    lambda r, a=a, b=b, bl=b_level: r["params"].get(a) == axes[a][0]
                    and r["params"].get(b) == bl
                )
                effects.append(
                    round(on - off, 6) if on is not None and off is not None else None
                )
            known = [e for e in effects if e is not None]
            sign_flip = len(known) == 2 and (known[0] > 0) != (known[1] > 0)
            interactions.append(
                {
                    "factor": a,
                    "conditioner": b,
                    "effect_by_conditioner_level": dict(
                        zip((str(v) for v in axes[b]), effects, strict=True)
                    ),
                    "sign_flip": sign_flip,
                }
            )

    top = max(valid, key=metric)
    return {
        "rank_by": rank_by,
        "factors": factors,
        "interactions": interactions,
        "top_params": top["params"],
        "top_metric": round(metric(top), 6),
        "read": (
            "Marginal effects are averaged across ALL cells (not just the "
            "winner); a compound proposal must cite them, and a factor whose "
            "marginal effect is negative does not ship unless a documented "
            "sign_flip interaction is the finding itself."
        ),
    }


def available_cpu_count() -> int:
    """Usable CPUs for backtest fan-out. Respects a cgroup CPU quota (Fly
    machines are quota-limited, and os.cpu_count() reports the host's cores,
    not the machine's) and an explicit WAYFINDER_MAX_BACKTEST_WORKERS override.
    Always ≥ 1."""
    override = os.environ.get("WAYFINDER_MAX_BACKTEST_WORKERS")
    if override:
        try:
            return max(1, int(override))
        except ValueError:
            pass
    # cgroup v2 quota → effective cores.
    try:
        quota, period = (
            open("/sys/fs/cgroup/cpu.max").read().split()
        )  # e.g. "200000 100000" → 2 cores; "max" → unlimited
        if quota != "max":
            return max(1, round(int(quota) / int(period)))
    except (OSError, ValueError):
        pass
    return max(1, os.cpu_count() or 1)


def _effective_workers(workers: int, parallel: str) -> int:
    """Clamp requested workers so a parameter sweep uses the box's cores fully
    but never oversubscribes them — the difference between "at CPU" and the
    thrash/peg of "out of CPU" on a small shared-vCPU machine. `workers <= 0`
    means "use all available cores"."""
    if parallel == "serial":
        return 1
    cap = available_cpu_count()
    if workers <= 0:
        return cap
    return max(1, min(workers, cap))


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
    check_rank_key(rank_by)
    match param_grid:
        case list():
            params_list = [dict(item) for item in param_grid]
        case _:
            keys = list(param_grid.keys())
            params_list = [
                dict(zip(keys, combo, strict=True))
                for combo in itertools.product(*(param_grid[key] for key in keys))
            ]
    grid_id = uuid.uuid4().hex[:12]
    # Never spawn more workers than the box has cores — oversubscribing a
    # 2-vCPU Fly machine pegs it (each process also reloads pandas + a copy of
    # the dataset). Threads don't help CPU-bound pandas (GIL); process is the
    # only real parallelism, and it's now bounded.
    workers = _effective_workers(workers, parallel)
    print(
        f"[grid] {len(params_list)} params · {parallel} · {workers} worker(s) "
        f"(of {available_cpu_count()} core(s))",
        file=sys.stderr,
        flush=True,
    )
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
    ranked, invalid = rank_and_partition(
        run_rows, rank_by=rank_by, top_n=top_n_artifacts
    )
    return ExecutionGridResult(
        grid_id=grid_id,
        rank_by=rank_by,
        runs=run_rows,
        ranked=ranked,
        invalid=invalid,
        search={
            "parallel": parallel,
            "workers": workers,
            "cpu_count": available_cpu_count(),
            "param_count": len(params_list),
        },
        plateau=(
            grid_plateau(run_rows, param_grid, rank_by=rank_by)
            if isinstance(param_grid, Mapping)
            else None
        ),
        factor_attribution=(
            grid_factor_attribution(run_rows, param_grid, rank_by=rank_by)
            if isinstance(param_grid, Mapping)
            else None
        ),
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
                json.dumps({**result.to_dict(), **stamp}, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
            with runs.open("w", encoding="utf-8") as handle:
                for row in result.runs:
                    handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
            return {"summary": str(summary), "runs": str(runs)}
        case _:
            latest = root / "latest.json"
            visualization = root / "visualization.json"
            # Compact separators: indent=2 on multi-MB per-bar arrays doubles
            # both the dump's transient memory and the disk footprint, and
            # these files are machine-read only.
            latest.write_text(
                json.dumps(
                    {**result.to_dict(), **stamp},
                    separators=(",", ":"),
                    default=str,
                )
                + "\n",
                encoding="utf-8",
            )
            visualization.write_text(
                json.dumps(result.visualization, separators=(",", ":"), default=str)
                + "\n",
                encoding="utf-8",
            )
            return {"latest": str(latest), "visualization": str(visualization)}


# module-level: ProcessPool pickling
def _process_run(
    payload: tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]],
) -> ExecutionBacktestResult:
    script_entrypoint, dataset_payload, spec, params = payload
    dataset = PreparedExecutionDataset.from_rows(
        dataset_payload["bars"], dataset_payload["metadata"]
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


def _mark_to_market(
    ledger: PositionLedger, close_by_symbol: Mapping[str, float]
) -> float:
    total = 0.0
    for position in ledger.positions.values():
        direction = 1 if position.side == "long" else -1
        close = (
            close_by_symbol[position.symbol]
            if position.symbol in close_by_symbol
            else position.avg_price
        )
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
    # Entry-price basis (deterministic from ledger snapshots, not marked).
    peak_notional = 0.0
    for row in positions or []:
        total = sum(
            float(record["size"]) * float(record["avg_price"])
            for record in row["positions"].values()
        )
        peak_notional = max(peak_notional, total)
    margin_used = peak_notional / leverage if peak_notional else 0.0
    exit_pnls = [
        float(trade["realized_pnl_delta"])
        for trade in trades
        if trade["reduce_only"] or trade["realized_pnl_delta"]
    ]
    trade_stats = _per_trade_stats(exit_pnls)
    durations = _trade_durations(trades)
    total_turnover = sum(
        abs(float(trade["filled_size"])) * float(trade["avg_price"] or 0.0)
        for trade in trades
    )
    liquidations = [
        event for event in guard_events or [] if event["kind"] == "liquidation"
    ]
    common = {
        "buy_hold_return": _buy_hold_return(price_series),
        "total_fees": sum(float(trade["fee"]) for trade in trades),
        "total_funding": sum(
            float(event["amount"])
            for event in guard_events or []
            if event["kind"] == "funding_applied"
        ),
        "total_turnover_usd": total_turnover,
        "liquidation_count": len(liquidations),
        "liquidated_at": liquidations[0]["timestamp"] if liquidations else None,
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
    exposed = sum(1 for row in positions if row["positions"]) if positions else 0
    if bar_seconds is None:
        bar_seconds = _inferred_bar_seconds(equity_curve)
    periods_per_year = SECONDS_PER_YEAR / bar_seconds if bar_seconds else None
    returns = _equity_returns(equity_curve)
    max_drawdown_pct = min((point["drawdown_pct"] for point in drawdowns), default=0.0)
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


def _sortino(returns: list[float], periods_per_year: float | None) -> float | None:
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
            win_rate - ((1 - win_rate) / win_loss_ratio) if win_loss_ratio > 0 else 0.0
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
    remaining: defaultdict[str, float] = defaultdict(float)
    spans: list[float] = []
    for trade in trades:
        symbol = trade["symbol"]
        raw_ts = trade["timestamp"]
        if not symbol or raw_ts is None:
            continue
        ts = pd.Timestamp(raw_ts)
        size = abs(float(trade["filled_size"]))
        if trade["reduce_only"]:
            if symbol in entry_ts:
                spans.append(float((ts - entry_ts[symbol]).total_seconds()))
            remaining[symbol] -= size
            if remaining[symbol] <= 1e-12:
                entry_ts.pop(symbol, None)
                remaining.pop(symbol, None)
        else:
            if remaining[symbol] <= 0.0:
                entry_ts[symbol] = ts
            remaining[symbol] += size
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
        closes = [float(point["close"]) for point in points]
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
    notional_by_ts: defaultdict[str, float] = defaultdict(float)
    for trade in trades:
        notional_by_ts[str(trade["timestamp"])] += abs(
            float(trade["filled_size"])
        ) * float(trade["avg_price"] or 0.0)
    ratios = [
        (notional_by_ts[str(row["timestamp"])] / float(row["equity"]))
        if float(row["equity"])
        else 0.0
        for row in equity_curve
    ]
    return sum(ratios) / len(ratios)


def _sharpe(
    equity_curve: list[dict[str, Any]], bar_seconds: int | None
) -> float | None:
    if len(equity_curve) < 3:
        return None
    returns = _equity_returns(equity_curve)
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
    timestamps = pd.to_datetime([row["timestamp"] for row in equity_curve], utc=True)
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
        equity = float(row["equity"])
        peak = equity if peak is None else max(peak, equity)
        drawdown = equity - peak
        drawdown_pct = drawdown / peak if peak else 0.0
        points.append(
            {
                "timestamp": row["timestamp"],
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


def _grid_row(result: ExecutionBacktestResult, *, rank_by: str) -> dict[str, Any]:
    # shared with optimize.py: grid and optuna rows must stay shape-identical
    # for rank_and_partition and experiments.jsonl
    return {
        "run_id": result.run_id,
        "params": result.params,
        "stats": result.stats,
        "validation": result.validation,
        rank_by: result.stats[rank_by],
    }
