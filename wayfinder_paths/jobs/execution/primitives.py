from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal

import pandas as pd

OrderAction = Literal["OPEN", "CLOSE", "STOP_LOSS", "TAKE_PROFIT", "CANCEL"]
FillStatus = Literal["filled", "partial", "resting", "rejected", "ambiguous"]
SnapshotStatus = Literal["valid", "ambiguous", "rate_limited", "stale"]


@dataclass
class ExecutionSpec:
    version: str = "1.0"
    venue: str = "hyperliquid"
    market_kind: str = "perp"
    view_type: str = "completed_bars"
    timeframe: str = "5m"
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
        }
    )
    state_model: str = "ledger_only"
    position_reconciliation: str = "required"
    validation: dict[str, Any] = field(
        default_factory=lambda: {"mode": "soft", "require_scenarios": False}
    )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> ExecutionSpec:
        payload = dict(data or {})
        spec = cls()
        for key in asdict(spec):
            if key in payload:
                setattr(spec, key, payload[key])
        defaults = cls()
        spec.ohlc_rules = {**defaults.ohlc_rules, **dict(spec.ohlc_rules or {})}
        spec.data_contract = {
            **defaults.data_contract,
            **dict(spec.data_contract or {}),
        }
        spec.validation = {**defaults.validation, **dict(spec.validation or {})}
        return spec

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def strict(self) -> bool:
        return str(self.validation.get("mode") or "").lower() == "strict"


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

    @classmethod
    def from_rows(cls, rows: list[Mapping[str, Any]]) -> CompletedBarsView:
        return cls(pd.DataFrame([dict(row) for row in rows]))

    @property
    def symbols(self) -> list[str]:
        return sorted(str(value) for value in self._bars["symbol"].unique())

    @property
    def timestamps(self) -> list[pd.Timestamp]:
        return list(pd.Index(self._bars["timestamp"].drop_duplicates()))

    def latest(self, symbol: str | None = None) -> dict[str, Any]:
        frame = self._filter_symbol(symbol)
        if frame.empty:
            raise ValueError("No completed bars available")
        return dict(frame.iloc[-1].to_dict())

    def window(self, n: int, symbol: str | None = None) -> CompletedBarsView:
        if n <= 0:
            raise ValueError("window size must be positive")
        frame = self._filter_symbol(symbol)
        if symbol is None:
            timestamps = frame["timestamp"].drop_duplicates().tail(n)
            frame = frame[frame["timestamp"].isin(timestamps)]
        else:
            frame = frame.tail(n)
        return CompletedBarsView(frame)

    def through(
        self, index_or_time: int | str | datetime | pd.Timestamp
    ) -> CompletedBarsView:
        match index_or_time:
            case int():
                timestamps = self.timestamps
                if not timestamps:
                    return CompletedBarsView(self._bars.iloc[0:0])
                index = min(max(index_or_time, 0), len(timestamps) - 1)
                cutoff = timestamps[index]
            case _:
                cutoff = pd.Timestamp(index_or_time)
                if cutoff.tzinfo is None:
                    cutoff = cutoff.tz_localize("UTC")
                else:
                    cutoff = cutoff.tz_convert("UTC")
        return CompletedBarsView(self._bars[self._bars["timestamp"] <= cutoff])

    def row_at(self, timestamp: pd.Timestamp, symbol: str | None = None) -> MarketBar:
        frame = self._bars[self._bars["timestamp"] == timestamp]
        if symbol is not None:
            frame = frame[frame["symbol"] == symbol]
        if frame.empty:
            raise ValueError(f"No bar at {timestamp} for {symbol or 'any symbol'}")
        row = frame.iloc[0]
        return MarketBar(
            timestamp=pd.Timestamp(row["timestamp"]),
            symbol=str(row["symbol"]),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=None if pd.isna(row["volume"]) else float(row["volume"]),
        )

    def to_frame(self) -> pd.DataFrame:
        return self._bars.copy()

    def to_rows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._bars.to_dict(orient="records")]

    def _filter_symbol(self, symbol: str | None) -> pd.DataFrame:
        if symbol is None:
            return self._bars
        return self._bars[self._bars["symbol"] == symbol]


@dataclass(frozen=True)
class EventMarketView:
    market_id: str
    outcome_id: str
    bid: float | None
    ask: float | None
    last: float | None
    liquidity: float | None = None
    volume_24h: float | None = None
    status: str = "open"
    resolution_time: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TokenState:
    token_id: str
    price: float | None = None
    market_cap: float | None = None
    volume_24h: float | None = None
    liquidity_proxy: float | None = None
    volatility_proxy: float | None = None
    trend_score: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


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
                    reduce_only=bool(data.get("reduce_only", False)),
                    client_order_id=data.get("client_order_id"),
                    bracket=dict(data.get("bracket") or {}) or None,
                    metadata=dict(data.get("metadata") or {}),
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
    view: CompletedBarsView | EventMarketView
    ledger: PositionLedger
    state_snapshot: StateSnapshot
    capacity: TradeCapacity | None
    params: dict[str, Any]
    timestamp: str
    execution_spec: ExecutionSpec


@dataclass
class ExecutionTrace:
    execution_spec: dict[str, Any]
    runs: list[dict[str, Any]] = field(default_factory=list)
    intents: list[dict[str, Any]] = field(default_factory=list)
    fills: list[dict[str, Any]] = field(default_factory=list)
    ledger_snapshots: list[dict[str, Any]] = field(default_factory=list)
    bracket_events: list[dict[str, Any]] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
