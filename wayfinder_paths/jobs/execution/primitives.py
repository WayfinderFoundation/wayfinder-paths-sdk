from __future__ import annotations

import bisect
import importlib.util
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

import pandas as pd

OrderAction = Literal["OPEN", "CLOSE", "STOP_LOSS", "TAKE_PROFIT", "CANCEL"]
FillStatus = Literal["filled", "partial", "resting", "rejected", "ambiguous"]
SnapshotStatus = Literal["valid", "ambiguous", "rate_limited", "stale", "risk_halt"]

DEFAULT_INITIAL_CAPITAL = 10_000.0


@dataclass
class ExecutionSpec:
    market_kind: str = "perp"
    view_type: str = "completed_bars"
    bar_model: str = "completed_only"
    fill_model: str = "next_bar_open"
    ohlc_rules: dict[str, Any] = field(
        default_factory=lambda: {
            "use_high_low_for_stops": True,
            "allow_close_only_entries": False,
            "same_bar_fill": False,
            "same_bar_policy": "conservative",
        }
    )
    data_contract: dict[str, Any] = field(
        default_factory=lambda: {
            "candles_source": "sdk_only",
            "no_external_ccxt": True,
            "rate_limit_safe": True,
            "bar_interval": None,
            "max_bar_age_intervals": 2,
            "stale_policy": "skip",
        }
    )
    validation: dict[str, Any] = field(
        default_factory=lambda: {"mode": "soft", "require_scenarios": False}
    )
    venues: list[str] = field(default_factory=lambda: ["hyperliquid"])

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> ExecutionSpec:
        payload = data or {}
        defaults = cls()
        spec = cls()
        for f in fields(cls):
            if f.name in payload:
                setattr(spec, f.name, payload[f.name])
        spec.ohlc_rules = {**defaults.ohlc_rules, **spec.ohlc_rules}
        spec.data_contract = {**defaults.data_contract, **spec.data_contract}
        spec.validation = {**defaults.validation, **spec.validation}
        return spec

    @classmethod
    def coerce(cls, value: ExecutionSpec | Mapping[str, Any] | None) -> ExecutionSpec:
        match value:
            case ExecutionSpec():
                return value
            case _:
                return cls.from_dict(value)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def strict(self) -> bool:
        return self.validation["mode"] == "strict"


@dataclass(frozen=True)
class MarketBar:
    timestamp: pd.Timestamp
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "symbol": self.symbol,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


class CompletedBarsView:
    """Immutable view over completed OHLC bars.

    The simulator hands strategies a truncated view at each tick. `to_frame()` is
    available for reporting and vector feature calculations, but callers should
    avoid mutating the returned copy.
    """

    REQUIRED_COLUMNS = {"timestamp", "symbol", "open", "high", "low", "close"}

    def __init__(self, bars: pd.DataFrame) -> None:
        frame = bars.copy()
        missing = self.REQUIRED_COLUMNS - set(frame.columns)
        if missing:
            raise ValueError(f"CompletedBarsView missing columns: {sorted(missing)}")
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        for column in ("open", "high", "low", "close"):
            frame[column] = pd.to_numeric(frame[column], errors="raise")
        if "volume" in frame.columns:
            frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce")
        else:
            frame["volume"] = None
        self._bars = frame.sort_values(["timestamp", "symbol"]).reset_index(drop=True)
        self._timestamps_cache: list[pd.Timestamp] | None = None
        self._symbols_cache: list[str] | None = None
        # {(ts, symbol): MarketBar} + {ts: first MarketBar at ts}; shared with
        # truncated child views (they guard lookups against their own bounds).
        self._row_index: dict[Any, MarketBar] | None = None

    @classmethod
    def from_rows(cls, rows: list[Mapping[str, Any]]) -> CompletedBarsView:
        return cls(pd.DataFrame([dict(row) for row in rows]))

    @classmethod
    def _from_trusted(
        cls,
        frame: pd.DataFrame,
        *,
        timestamps: list[pd.Timestamp] | None = None,
        row_index: dict[Any, MarketBar] | None = None,
    ) -> CompletedBarsView:
        """Fast path for frames already coerced+sorted by a prior __init__
        (e.g. per-tick truncation). Skipping re-coercion turns the simulator's
        per-bar view construction from O(n) coercions into a plain slice.
        Passing the parent's timestamp slice and row index makes per-tick
        views O(1) instead of recomputing uniques/masks each bar."""
        view = object.__new__(cls)
        view._bars = frame
        view._timestamps_cache = timestamps
        view._symbols_cache = None
        view._row_index = row_index
        return view

    @property
    def symbols(self) -> list[str]:
        if self._symbols_cache is None:
            self._symbols_cache = sorted(
                str(value) for value in self._bars["symbol"].unique()
            )
        return list(self._symbols_cache)

    @property
    def timestamps(self) -> list[pd.Timestamp]:
        return list(self._ensure_timestamps())

    def _ensure_timestamps(self) -> list[pd.Timestamp]:
        if self._timestamps_cache is None:
            self._timestamps_cache = list(
                pd.Index(self._bars["timestamp"].drop_duplicates())
            )
        return self._timestamps_cache

    def _ensure_row_index(self) -> dict[Any, MarketBar]:
        if self._row_index is None:
            index: dict[Any, MarketBar] = {}
            for row in self._bars.itertuples(index=False):
                bar = MarketBar(
                    timestamp=pd.Timestamp(row.timestamp),
                    symbol=str(row.symbol),
                    open=float(row.open),
                    high=float(row.high),
                    low=float(row.low),
                    close=float(row.close),
                    volume=None if pd.isna(row.volume) else float(row.volume),
                )
                index[(bar.timestamp, bar.symbol)] = bar
                # First row at each timestamp (frame sorted by ts, symbol).
                index.setdefault(bar.timestamp, bar)
            self._row_index = index
        return self._row_index

    def latest(self, symbol: str | None = None) -> dict[str, Any]:
        frame = self._filter_symbol(symbol)
        if frame.empty:
            raise ValueError("No completed bars available")
        return frame.iloc[-1].to_dict()

    def feature(self, name: str, symbol: str | None = None) -> Any:
        """Latest non-null value of an exogenous feature column (merged by
        the driver/dataset loader per execution_spec.data_contract.features).
        Pure — reads only view data, purity-sandbox safe."""
        frame = self._filter_symbol(symbol)
        if name not in frame.columns:
            raise ValueError(f"No feature column {name!r} in view")
        series = frame[name].dropna()
        if series.empty:
            raise ValueError(f"No values yet for feature {name!r}")
        return series.iloc[-1]

    def through(
        self, index_or_time: int | str | datetime | pd.Timestamp
    ) -> CompletedBarsView:
        timestamps = self._ensure_timestamps()
        match index_or_time:
            case int():
                if not timestamps:
                    return CompletedBarsView._from_trusted(self._bars.iloc[0:0])
                index = min(max(index_or_time, 0), len(timestamps) - 1)
            case _:
                cutoff = pd.Timestamp(index_or_time)
                if cutoff.tzinfo is None:
                    cutoff = cutoff.tz_localize("UTC")
                else:
                    cutoff = cutoff.tz_convert("UTC")
                index = bisect.bisect_right(timestamps, cutoff) - 1
                if index < 0:
                    return CompletedBarsView._from_trusted(self._bars.iloc[0:0])
        return self._slice(0, index)

    def window(self, index: int, lookback_bars: int) -> CompletedBarsView:
        """Trailing window ending at timestamp `index`, at most `lookback_bars`
        timestamps deep — the same bounded history the live driver fetches."""
        timestamps = self._ensure_timestamps()
        if not timestamps:
            return CompletedBarsView._from_trusted(self._bars.iloc[0:0])
        index = min(max(index, 0), len(timestamps) - 1)
        start = max(0, index - max(int(lookback_bars), 1) + 1)
        return self._slice(start, index)

    def _slice(self, start: int, end: int) -> CompletedBarsView:
        """View over timestamps[start..end] inclusive. The sorted frame makes
        this a contiguous positional slice; children inherit the timestamp
        slice and the shared row index (bounds-guarded in row_at)."""
        timestamps = self._ensure_timestamps()
        column = self._bars["timestamp"]
        start_pos = (
            0 if start == 0 else int(column.searchsorted(timestamps[start], side="left"))
        )
        end_pos = int(column.searchsorted(timestamps[end], side="right"))
        return CompletedBarsView._from_trusted(
            self._bars.iloc[start_pos:end_pos],
            timestamps=timestamps[start : end + 1],
            row_index=self._ensure_row_index(),
        )

    def row_at(self, timestamp: pd.Timestamp, symbol: str | None = None) -> MarketBar:
        timestamps = self._ensure_timestamps()
        try:
            in_bounds = bool(
                timestamps and timestamps[0] <= timestamp <= timestamps[-1]
            )
        except TypeError:  # uncomparable input (naive ts, junk) == no bar
            in_bounds = False
        if in_bounds:
            key = (timestamp, symbol) if symbol is not None else timestamp
            bar = self._ensure_row_index().get(key)
            if bar is not None:
                # MarketBar is frozen, so sharing index instances across
                # callers is safe — no defensive copy needed.
                return bar
        raise ValueError(f"No bar at {timestamp} for {symbol or 'any symbol'}")

    def __len__(self) -> int:
        return len(self._bars)

    def to_frame(self) -> pd.DataFrame:
        return self._bars.copy()

    def symbol_frame(self, symbol: str) -> pd.DataFrame:
        """Rows for one symbol WITHOUT the defensive whole-frame copy of
        to_frame(). Callers must treat the result as read-only."""
        return self._bars[self._bars["symbol"] == symbol]

    def to_rows(self) -> list[dict[str, Any]]:
        return self._bars.to_dict(orient="records")

    def _filter_symbol(self, symbol: str | None) -> pd.DataFrame:
        if symbol is None:
            return self._bars
        return self._bars[self._bars["symbol"] == symbol]


@dataclass
class OrderIntent:
    action: OrderAction
    venue: str
    symbol: str
    side: str
    size: float | None = None
    notional: float | None = None
    reduce_only: bool = False
    client_order_id: str | None = None
    bracket: dict[str, Any] | None = None
    limit_price: float | None = None
    expires_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_any(cls, value: OrderIntent | Mapping[str, Any]) -> OrderIntent:
        match value:
            case OrderIntent():
                return value
            case _:
                data = dict(value)
                return cls(
                    action=str(data.get("action") or "").upper(),  # type: ignore[arg-type]
                    venue=str(data.get("venue") or "hyperliquid"),
                    symbol=str(data.get("symbol") or data.get("market_id") or ""),
                    side=str(data.get("side") or ""),
                    size=_float_or_none(data.get("size")),
                    notional=_float_or_none(data.get("notional")),
                    reduce_only=bool(data.get("reduce_only")),
                    client_order_id=data.get("client_order_id"),
                    bracket=dict(data["bracket"]) if data.get("bracket") else None,
                    limit_price=_float_or_none(data.get("limit_price")),
                    expires_at=data.get("expires_at"),
                    metadata=dict(data["metadata"]) if data.get("metadata") else {},
                )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FillEvent:
    status: FillStatus
    venue: str
    symbol: str
    side: str
    filled_size: float = 0.0
    avg_price: float | None = None
    fee: float = 0.0
    order_id: str | None = None
    client_order_id: str | None = None
    reduce_only: bool = False
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    timestamp: str | None = None

    @property
    def successful(self) -> bool:
        return self.status == "filled" and self.filled_size > 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StateSnapshot:
    status: SnapshotStatus = "valid"
    reason: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def usable_for_state_clear(self) -> bool:
        return self.status == "valid"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TradeCapacity:
    max_notional: float | None = None
    available_margin: float | None = None
    max_position_size: float | None = None
    safe: bool = False
    source: str = "unknown"
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PositionRecord:
    symbol: str
    side: str
    size: float
    avg_price: float
    bars_held: int = 0
    opened_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PositionLedger:
    def __init__(self) -> None:
        self.positions: dict[str, PositionRecord] = {}
        self.realized_pnl: float = 0.0
        self._last_bar_time: str | None = None

    def apply_fill(self, fill: FillEvent) -> None:
        if not fill.successful or fill.avg_price is None:
            return
        size = abs(float(fill.filled_size))
        side = _normalize_side(fill.side)
        existing = self.positions.get(fill.symbol)
        if fill.reduce_only or fill.side.lower() in {
            "close",
            "sell_close",
            "buy_close",
        }:
            if existing is None:
                return
            close_size = min(size, existing.size)
            direction = 1 if existing.side == "long" else -1
            self.realized_pnl += (
                direction * (fill.avg_price - existing.avg_price) * close_size
            )
            remaining = existing.size - close_size
            if remaining <= 1e-12:
                self.positions.pop(fill.symbol, None)
            else:
                existing.size = remaining
            return
        if existing is None or existing.side != side:
            self.positions[fill.symbol] = PositionRecord(
                symbol=fill.symbol,
                side=side,
                size=size,
                avg_price=float(fill.avg_price),
                opened_at=fill.timestamp,
            )
            return
        total = existing.size + size
        existing.avg_price = (
            existing.avg_price * existing.size + float(fill.avg_price) * size
        ) / total
        existing.size = total

    def on_bar_tick(self, bar_time: Any) -> None:
        value = str(bar_time)
        if value == self._last_bar_time:
            return
        self._last_bar_time = value
        for position in self.positions.values():
            position.bars_held += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "positions": {
                symbol: record.to_dict() for symbol, record in self.positions.items()
            },
            "realized_pnl": self.realized_pnl,
            "last_bar_time": self._last_bar_time,
        }

    @classmethod
    def restore(cls, data: Mapping[str, Any] | None) -> PositionLedger:
        ledger = cls()
        payload = data or {}
        for symbol, record in (payload.get("positions") or {}).items():
            ledger.positions[str(symbol)] = PositionRecord(
                symbol=str(record.get("symbol") or symbol),
                side=str(record.get("side") or "long"),
                size=float(record.get("size") or 0.0),
                avg_price=float(record.get("avg_price") or 0.0),
                bars_held=int(record.get("bars_held") or 0),
                opened_at=record.get("opened_at"),
                metadata=dict(record.get("metadata") or {}),
            )
        ledger.realized_pnl = float(payload.get("realized_pnl") or 0.0)
        ledger._last_bar_time = payload.get("last_bar_time")
        return ledger


class BracketEngine:
    @staticmethod
    def ohlc_stop_hit(
        bar: Mapping[str, Any] | MarketBar, side: str, level: float
    ) -> bool:
        low = _bar_value(bar, "low")
        high = _bar_value(bar, "high")
        return low <= level if _normalize_side(side) == "long" else high >= level

    @staticmethod
    def ohlc_take_profit_hit(
        bar: Mapping[str, Any] | MarketBar, side: str, level: float
    ) -> bool:
        low = _bar_value(bar, "low")
        high = _bar_value(bar, "high")
        return high >= level if _normalize_side(side) == "long" else low <= level

    @staticmethod
    def resolve_intrabar(
        bar: Mapping[str, Any] | MarketBar,
        side: str,
        stop_loss: float | None,
        take_profit: float | None,
        policy: str = "conservative",
    ) -> dict[str, Any]:
        stop_hit = stop_loss is not None and BracketEngine.ohlc_stop_hit(
            bar, side, stop_loss
        )
        tp_hit = take_profit is not None and BracketEngine.ohlc_take_profit_hit(
            bar, side, take_profit
        )
        if stop_hit and tp_hit:
            exit_type = "STOP_LOSS" if policy == "conservative" else "TAKE_PROFIT"
            price = stop_loss if exit_type == "STOP_LOSS" else take_profit
            hit, ambiguous = True, True
        elif stop_hit:
            exit_type, price, hit, ambiguous = "STOP_LOSS", stop_loss, True, False
        elif tp_hit:
            exit_type, price, hit, ambiguous = "TAKE_PROFIT", take_profit, True, False
        else:
            exit_type, price, hit, ambiguous = None, None, False, False
        return {
            "hit": hit,
            "exit_type": exit_type,
            "price": price,
            "ambiguous": ambiguous,
            "policy": policy,
            "used_ohlc": True,
        }


@dataclass
class ExecutionContext:
    """Everything decide() may read. `strategy_state` is the strategy's own
    scratch store — the engine persists it across ticks (and replays it in
    reconciliation), so values must be JSON-serializable; decide() mutates it
    in place."""

    view: CompletedBarsView
    ledger: PositionLedger
    state_snapshot: StateSnapshot
    capacity: TradeCapacity | None
    params: dict[str, Any]
    timestamp: str
    execution_spec: ExecutionSpec
    strategy_state: dict[str, Any] = field(default_factory=dict)


def mark_to_market_equity(ctx: ExecutionContext) -> float:
    """Current equity as decide() can see it: initial capital + realized PnL +
    unrealized mark-to-market at the latest completed close. Pure (ctx data
    only — purity-sandbox safe) and bar-identical to the simulator's equity
    curve, so compound sizing in backtest and live use the same number."""
    equity = (
        float(ctx.params.get("initial_capital") or DEFAULT_INITIAL_CAPITAL)
        + ctx.ledger.realized_pnl
    )
    for position in ctx.ledger.positions.values():
        try:
            close = float(ctx.view.latest(position.symbol)["close"])
        except ValueError:
            close = position.avg_price
        direction = 1 if position.side == "long" else -1
        equity += direction * (close - position.avg_price) * position.size
    return equity


@dataclass
class ExecutionTrace:
    execution_spec: dict[str, Any]
    runs: list[dict[str, Any]] = field(default_factory=list)
    intents: list[dict[str, Any]] = field(default_factory=list)
    fills: list[dict[str, Any]] = field(default_factory=list)
    ledger_snapshots: list[dict[str, Any]] = field(default_factory=list)
    bracket_events: list[dict[str, Any]] = field(default_factory=list)
    guard_events: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _load_module_from_path(path: Path) -> ModuleType:
    module_name = f"_wayfinder_execution_module_{abs(hash(str(path.resolve())))}"
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
    return module


def _normalize_side(side: str) -> str:
    raw = str(side).lower()
    if raw in {"short", "sell"}:
        return "short"
    return "long"


def _bar_value(bar: Mapping[str, Any] | MarketBar, key: str) -> float:
    match bar:
        case MarketBar():
            return float(getattr(bar, key))
        case _:
            return float(bar[key])


def _float_or_none(value: Any) -> float | None:
    return float(value) if value is not None else None


_BAR_INTERVAL_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def bar_interval_seconds(value: Any) -> int | None:
    """Parse a bar interval like "5m", "1h", or plain seconds into seconds."""
    match value:
        case None:
            return None
        case int() | float():
            return int(value) if value > 0 else None
    text = str(value).strip().lower()
    if not text:
        return None
    unit = text[-1]
    if unit in _BAR_INTERVAL_UNITS and text[:-1].isdigit():
        count = int(text[:-1])
        return count * _BAR_INTERVAL_UNITS[unit] if count > 0 else None
    return int(text) if text.isdigit() and int(text) > 0 else None
