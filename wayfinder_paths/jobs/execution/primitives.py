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

import numpy as np
import pandas as pd

OrderAction = Literal["OPEN", "CLOSE", "STOP_LOSS", "TAKE_PROFIT", "CANCEL"]
FillStatus = Literal["filled", "partial", "resting", "rejected", "ambiguous"]
SnapshotStatus = Literal["valid", "ambiguous", "rate_limited", "stale", "risk_halt"]

DEFAULT_INITIAL_CAPITAL = 10_000.0
BAR_CLOSE_LABEL = "close_time"


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


_NO_DEFAULT = object()


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
        # int64-ns timestamps of THIS frame's rows (sorted). row_at() binary-
        # searches this instead of a {key: MarketBar} dict index — the dict
        # retained one dataclass per row (115MB across rebuilds at the 120d
        # 4-symbol scale, the gate backtest's OOM driver on the 2GB box).
        self._ts_ns_cache: np.ndarray | None = None
        # {symbol: sorted positional indices in THIS frame}; shared with child
        # views alongside this view's absolute offset, so symbol_frame() is an
        # integer take instead of a per-call string-equality mask.
        self._symbol_positions: dict[str, np.ndarray] | None = None
        self._symbol_positions_offset: int = 0

    @classmethod
    def from_rows(cls, rows: list[Mapping[str, Any]]) -> CompletedBarsView:
        # Columnar extraction, not [dict(row) for row in rows]: the per-row
        # copy doubled the 120d/4-symbol parse footprint (a second 138k-dict
        # list co-resident with the json.loads output) on the 2GB box.
        if not rows:
            return cls(pd.DataFrame(columns=sorted(cls.REQUIRED_COLUMNS)))
        columns = {key: [row.get(key) for row in rows] for key in rows[0].keys()}
        return cls(pd.DataFrame(columns))

    @classmethod
    def _from_trusted(
        cls,
        frame: pd.DataFrame,
        *,
        timestamps: list[pd.Timestamp] | None = None,
        ts_ns: np.ndarray | None = None,
        symbol_positions: dict[str, np.ndarray] | None = None,
        symbol_positions_offset: int = 0,
    ) -> CompletedBarsView:
        """Fast path for frames already coerced+sorted by a prior __init__
        (e.g. per-tick truncation). Skipping re-coercion turns the simulator's
        per-bar view construction from O(n) coercions into a plain slice.
        Passing the parent's timestamp slice and ts-ns array makes per-tick
        views O(1) instead of recomputing uniques/masks each bar."""
        view = object.__new__(cls)
        view._bars = frame
        view._timestamps_cache = timestamps
        view._symbols_cache = None
        view._ts_ns_cache = ts_ns
        view._symbol_positions = symbol_positions
        view._symbol_positions_offset = symbol_positions_offset
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

    def _ensure_ts_ns(self) -> np.ndarray:
        if self._ts_ns_cache is None:
            self._ts_ns_cache = (
                self._bars["timestamp"].to_numpy(dtype="datetime64[ns]").astype("int64")
            )
        return self._ts_ns_cache

    def _bar_at_position(self, position: int) -> MarketBar:
        row = self._bars.iloc[position]
        volume = row["volume"]
        return MarketBar(
            timestamp=pd.Timestamp(row["timestamp"]),
            symbol=str(row["symbol"]),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=None if pd.isna(volume) else float(volume),
        )

    def latest(self, symbol: str | None = None) -> dict[str, Any]:
        frame = self._filter_symbol(symbol)
        if frame.empty:
            raise ValueError("No completed bars available")
        return frame.iloc[-1].to_dict()

    def _filter_symbol(self, symbol: str | None) -> pd.DataFrame:
        if symbol is None:
            return self._bars
        return self.symbol_frame(symbol)

    def _ensure_symbol_positions(self) -> dict[str, np.ndarray]:
        if self._symbol_positions is None:
            codes, uniques = pd.factorize(self._bars["symbol"])
            self._symbol_positions = {
                str(uniques[i]): np.flatnonzero(codes == i) for i in range(len(uniques))
            }
            self._symbol_positions_offset = 0
        return self._symbol_positions

    def feature(
        self, name: str, symbol: str | None = None, *, default: Any = _NO_DEFAULT
    ) -> Any:
        """Latest non-null value of an exogenous feature column (merged by
        the driver/dataset loader per execution_spec.data_contract.features).
        Pure — reads only view data, purity-sandbox safe.

        Derived columns start later than the price history (the macro regime
        needs 28 days, the leader state 7): pass ``default`` for the bars
        before the first value. An undeclared column raises regardless — that
        is a contract error, not missing history."""
        frame = self._filter_symbol(symbol)
        if name not in frame.columns:
            raise ValueError(f"No feature column {name!r} in view")
        series = frame[name].dropna()
        if series.empty:
            if default is not _NO_DEFAULT:
                return default
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
            0
            if start == 0
            else int(column.searchsorted(timestamps[start], side="left"))
        )
        end_pos = int(column.searchsorted(timestamps[end], side="right"))
        return CompletedBarsView._from_trusted(
            self._bars.iloc[start_pos:end_pos],
            timestamps=timestamps[start : end + 1],
            ts_ns=self._ensure_ts_ns()[start_pos:end_pos],
            symbol_positions=self._ensure_symbol_positions(),
            symbol_positions_offset=self._symbol_positions_offset + start_pos,
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
            try:
                ts_ns = int(pd.Timestamp(timestamp).value)
            except (TypeError, ValueError):
                ts_ns = None
            if ts_ns is not None:
                column = self._ensure_ts_ns()
                left = int(np.searchsorted(column, ts_ns, side="left"))
                right = int(np.searchsorted(column, ts_ns, side="right"))
                for position in range(left, right):
                    if symbol is None or str(self._bars["symbol"].iat[position]) == str(
                        symbol
                    ):
                        return self._bar_at_position(position)
        raise ValueError(f"No bar at {timestamp} for {symbol or 'any symbol'}")

    def __len__(self) -> int:
        return len(self._bars)

    def to_frame(self) -> pd.DataFrame:
        return self._bars.copy()

    def symbol_frame(self, symbol: str) -> pd.DataFrame:
        """Rows for one symbol WITHOUT the defensive whole-frame copy of
        to_frame(). Callers must treat the result as read-only.

        Positional take over cached per-symbol indices (shared root→child via
        _from_trusted) instead of a per-call string-equality mask — the mask
        made this the top engine cost for multi-symbol strategies (14 symbols
        = 14 full-window scans per tick)."""
        positions = self._ensure_symbol_positions()
        pos = positions.get(symbol)
        if pos is None and not isinstance(symbol, str):
            pos = positions.get(str(symbol))
        if pos is None or not len(pos):
            return self._bars.iloc[0:0]
        offset = self._symbol_positions_offset
        lo = int(np.searchsorted(pos, offset, side="left"))
        hi = int(np.searchsorted(pos, offset + len(self._bars), side="left"))
        window_pos = pos[lo:hi]
        if offset:
            window_pos = window_pos - offset
        return self._bars.iloc[window_pos]

    def to_rows(self) -> list[dict[str, Any]]:
        return self._bars.to_dict(orient="records")


# Default per-bar compute window. Bounding the view handed to each decide()
# keeps the DEFAULT backtest O(N·k) instead of O(N²): a strategy that
# recomputes indicators over the whole handed frame goes quadratic when that
# frame grows with the replay index (the classic "simple backtest pegs the
# CPU" trap). 512 bars covers the lookback of essentially every standard
# indicator (SMA200, ATR/ADX, long EMAs) with margin. Strategies tune it via
# `warmup_bars`; genuine since-genesis strategies opt out with
# `full_history: true`.
DEFAULT_WARMUP_BARS = 512

# A full_history strategy still needs a concrete live fetch depth (no feed
# pages "everything since genesis" each tick); 200 bars is the legacy driver
# default, so pre-contract live jobs keep their exact fetch size.
FULL_HISTORY_LIVE_DEPTH_BARS = 200

_DECLARED_WINDOW_SOURCES = frozenset(
    {"warmup_bars", "lookback_bars", "strategy.warmup_bars"}
)


@dataclass(frozen=True)
class ComputeWindow:
    """The single decide() history contract shared by the backtest simulator,
    the live driver, and the candidate shadow replayer.

    One resolution + one slice makes backtest inputs ≡ forward inputs by
    construction: whatever window a strategy declares is exactly the history
    every execution path hands it."""

    size: int | None  # None ⇒ full history (explicit `full_history` opt-out)
    source: str
    live_depth: int  # completed bars a live/shadow fetch loads each tick

    @property
    def full_history(self) -> bool:
        return self.size is None

    @property
    def declared(self) -> bool:
        """True when the window was declared (params or strategy attribute)
        rather than defaulted — the precondition for the window-invariance
        probe and for evolution-campaign admission."""
        return self.source in _DECLARED_WINDOW_SOURCES

    def slice_view(self, bars: CompletedBarsView, index: int) -> CompletedBarsView:
        """Causal view for a decision at timestamp `index`: at most `size`
        trailing timestamps, or everything through `index` for full history."""
        if self.size is None:
            return bars.through(index)
        return bars.window(index, self.size)


def resolve_compute_window(
    params: Mapping[str, Any], strategy: Any = None
) -> ComputeWindow:
    """Size of the trailing view handed to decide() each tick.

    Resolution (first hit wins):
      1. ``params['warmup_bars']``   — explicit, canonical name.
      2. ``params['lookback_bars']`` — back-compat with the old windowing lever.
      3. ``strategy.warmup_bars``    — strategy-declared attribute.
      4. ``DEFAULT_WARMUP_BARS``.
    ``params['full_history']`` truthy opts backtests into full-history views;
    live fetches then fall back to the legacy driver depth (``lookback_bars``
    or ``FULL_HISTORY_LIVE_DEPTH_BARS``).
    """
    if params.get("full_history"):
        live_depth = max(
            int(params.get("lookback_bars") or FULL_HISTORY_LIVE_DEPTH_BARS), 1
        )
        return ComputeWindow(size=None, source="full_history", live_depth=live_depth)
    for key in ("warmup_bars", "lookback_bars"):
        raw = params.get(key)
        if raw:
            size = max(int(raw), 1)
            return ComputeWindow(size=size, source=key, live_depth=size)
    attr = getattr(strategy, "warmup_bars", None)
    if attr:
        size = max(int(attr), 1)
        return ComputeWindow(size=size, source="strategy.warmup_bars", live_depth=size)
    return ComputeWindow(
        size=DEFAULT_WARMUP_BARS, source="default", live_depth=DEFAULT_WARMUP_BARS
    )


# Actions that only ever shrink an existing position — never sized up by
# engine-level leverage, never blocked as fresh exposure.
REDUCE_ONLY_ACTIONS = frozenset({"CLOSE", "STOP_LOSS", "TAKE_PROFIT"})


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
    time_in_force: str | None = None
    expires_after_bars: int | None = None
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
                    time_in_force=(
                        str(data["time_in_force"])
                        if data.get("time_in_force") is not None
                        else None
                    ),
                    expires_after_bars=(
                        int(data["expires_after_bars"])
                        if data.get("expires_after_bars") is not None
                        else None
                    ),
                    metadata=dict(data["metadata"]) if data.get("metadata") else {},
                )

    def to_dict(self) -> dict[str, Any]:
        # Manual dict: asdict() deep-copies recursively and was a measured
        # per-tick engine cost (trace/rows serialize intents and fills).
        return {
            "action": self.action,
            "venue": self.venue,
            "symbol": self.symbol,
            "side": self.side,
            "size": self.size,
            "notional": self.notional,
            "reduce_only": self.reduce_only,
            "client_order_id": self.client_order_id,
            "bracket": dict(self.bracket) if self.bracket else self.bracket,
            "limit_price": self.limit_price,
            "time_in_force": self.time_in_force,
            "expires_after_bars": self.expires_after_bars,
            "metadata": dict(self.metadata),
        }


def is_risk_reducing_intent(intent: OrderIntent) -> bool:
    """Recognize semantic exits even when a strategy omits `reduce_only`."""
    return intent.reduce_only or str(intent.action).upper() in REDUCE_ONLY_ACTIONS


@dataclass
class RestingOrder:
    """Durable state for a submitted limit order awaiting fills or expiry."""

    intent: OrderIntent
    submitted_at: str
    age_bars: int = 0
    order_id: str | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RestingOrder:
        return cls(
            intent=OrderIntent.from_any(data.get("intent") or {}),
            submitted_at=str(data.get("submitted_at") or ""),
            age_bars=int(data.get("age_bars") or 0),
            order_id=(str(data["order_id"]) if data.get("order_id") else None),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent.to_dict(),
            "submitted_at": self.submitted_at,
            "age_bars": self.age_bars,
            "order_id": self.order_id,
        }


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
        return {
            "status": self.status,
            "venue": self.venue,
            "symbol": self.symbol,
            "side": self.side,
            "filled_size": self.filled_size,
            "avg_price": self.avg_price,
            "fee": self.fee,
            "order_id": self.order_id,
            "client_order_id": self.client_order_id,
            "reduce_only": self.reduce_only,
            "error": self.error,
            "raw": dict(self.raw),
            "timestamp": self.timestamp,
        }


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
        # Ledger snapshots run up to 3x per tick over every open position —
        # asdict() here was ~20% of total engine time on multi-leg strategies.
        return {
            "symbol": self.symbol,
            "side": self.side,
            "size": self.size,
            "avg_price": self.avg_price,
            "bars_held": self.bars_held,
            "opened_at": self.opened_at,
            "metadata": dict(self.metadata),
        }


class PositionLedger:
    def __init__(self) -> None:
        self.positions: dict[str, PositionRecord] = {}
        self.realized_pnl: float = 0.0
        self._last_bar_time: str | None = None

    def apply_fill(self, fill: FillEvent) -> None:
        if not fill.successful or fill.avg_price is None:
            return
        # Trading fees are a real cost of every fill (paid on entry AND exit),
        # so charge them to realized PnL. Previously the fee was computed on the
        # fill but never deducted here — it only surfaced as a separate
        # `total_fees` stat, so the equity curve, net_return, and per-trade PnL
        # were all reported GROSS of fees. That made small-edge strategies
        # (e.g. Hyperliquid scalpers) look profitable in backtest while bleeding
        # fees live. Deducting here flows through to every headline metric,
        # since equity = initial_capital + realized_pnl + unrealized and
        # realized_pnl_delta is the change in realized_pnl across the fill.
        self.realized_pnl -= float(fill.fee or 0.0)
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
        """Rebuild from a prior `snapshot()` payload (None = fresh ledger)."""
        ledger = cls()
        if not data:
            return ledger
        for symbol, record in data["positions"].items():
            ledger.positions[str(symbol)] = PositionRecord(
                symbol=record["symbol"],
                side=record["side"],
                size=float(record["size"]),
                avg_price=float(record["avg_price"]),
                bars_held=int(record["bars_held"]),
                opened_at=record["opened_at"],
                metadata=dict(record["metadata"]),
            )
        ledger.realized_pnl = float(data["realized_pnl"])
        ledger._last_bar_time = data["last_bar_time"]
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
        normalized_side = _normalize_side(side)
        open_price = _bar_value(bar, "open")
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
        trigger_price = price
        gap_at_open = False
        if exit_type == "STOP_LOSS" and stop_loss is not None:
            gap_at_open = (
                open_price < stop_loss
                if normalized_side == "long"
                else open_price > stop_loss
            )
            if gap_at_open:
                # A stop-market order cannot fill at a price the market gapped
                # through. Use the first observable price, then let the broker's
                # stop-specific slippage model apply on top.
                price = open_price
        return {
            "hit": hit,
            "exit_type": exit_type,
            "price": price,
            "trigger_price": trigger_price,
            "open_price": open_price,
            "gap_at_open": gap_at_open,
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
    resting_orders: tuple[RestingOrder, ...] = ()

    @property
    def bar_index(self) -> int:
        """1-based count of completed timestamps in the handed view.

        Good for WARMUP gates (`if ctx.bar_index < warmup_bars`): it measures
        data actually available, cheaply (no DataFrame touched). NOT a
        cadence clock — live hands a sliding fixed-length window, so this
        stays constant tick after tick and `bar_index % n` silently never
        (or always) fires. Use `ctx.every_n_bars(n)` for cadence. NOT a clock
        either: never stamp it into strategy_state to measure an age, cooldown
        or expiry — the view length is constant once warm, so
        `ctx.bar_index - stored` reads 0 forever and an armed state machine
        never fires. Use `ctx.bar_ordinal` / `ctx.bars_since(stamp)`."""
        return len(self.view._ensure_timestamps())

    @property
    def bar_ordinal(self) -> int | None:
        """Global position of the latest completed bar: timestamp // interval.

        Advances one per bar in backtest AND live (the sliding window's END
        moves), survives restarts, and is a plain int, so stamp it into
        strategy_state for ages, cooldowns, TTLs and expiries and measure with
        `ctx.bars_since(stamp)`. None without a declared bar_interval or an
        empty view. A data gap counts as elapsed bars (wall-clock semantics).
        """
        timestamps = self.view._ensure_timestamps()
        if not timestamps:
            return None
        interval = bar_interval_seconds(
            self.execution_spec.data_contract.get("bar_interval")
        )
        if not interval:
            return None
        return int(timestamps[-1].timestamp() // interval)

    def bars_since(self, stamp: Any) -> int | None:
        """Bars elapsed since a stored `bar_ordinal`; None when either side is
        missing."""
        ordinal = self.bar_ordinal
        if ordinal is None or stamp is None:
            return None
        return ordinal - int(stamp)

    def every_n_bars(self, n: int, *, offset: int = 0) -> bool:
        """Epoch-aligned cadence gate: True when the latest completed bar's
        position in the GLOBAL bar sequence (timestamp // bar_interval) is
        congruent to `offset` mod n. Identical in backtest and live, and
        restart-proof — never count ticks in `strategy_state` for warmup or
        cadence (a state reset re-warms the counter and the job goes dark
        for a full warmup period). `offset` pins WHICH bars fire: a strategy
        validated on a particular rebalance phase keeps that exact schedule
        (phase can matter — a one-day shift materially changed a break-even
        daily basket's 4-year path). For elapsed time (an age, cooldown or
        expiry) use `bar_ordinal` / `bars_since`."""
        if n <= 1:
            return True
        if not self.view._ensure_timestamps():
            return False
        ordinal = self.bar_ordinal
        if ordinal is None:
            return True
        return ordinal % n == offset % n


def mark_to_market_equity(ctx: ExecutionContext) -> float:
    """Current equity as decide() can see it. In LIVE mode the reconcile step
    puts the venue's marked account value on `state_snapshot.data` and that is
    authoritative (it already embeds realized + unrealized PnL) — sizing from
    config capital nearly fired ~$8k orders on a $29.50 account. Backtests
    never populate snapshot data, so they keep the config-capital arithmetic:
    initial capital + realized PnL + unrealized mark-to-market at the latest
    completed close. Pure (ctx data only — purity-sandbox safe)."""
    live_account_value = (ctx.state_snapshot.data or {}).get("account_value")
    if live_account_value is not None and float(live_account_value) > 0:
        return float(live_account_value)
    equity = (
        float(ctx.params.get("initial_capital") or DEFAULT_INITIAL_CAPITAL)
        + ctx.ledger.realized_pnl
    )
    for position in ctx.ledger.positions.values():
        frame = ctx.view.symbol_frame(position.symbol)
        close = (
            float(frame["close"].iloc[-1]) if not frame.empty else position.avg_price
        )
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
    # Register BEFORE exec: stdlib machinery resolves cls.__module__ through
    # sys.modules — without this, a @dataclass defined in a workspace module
    # dies inside dataclasses._process_class (hit live: an agent's signals.py
    # using a dataclass crashed the campaign scan).
    sys.modules[module_name] = module
    try:
        sys.path.insert(0, str(path.parent))
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
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
