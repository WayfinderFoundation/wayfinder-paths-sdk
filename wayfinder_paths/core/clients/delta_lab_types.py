"""Typed records and error types for the Delta Lab client.

Each typed record stores the fields we've observed on the wire plus a `raw`
dict that preserves the full server response. Callers can access forward-compat
fields via `record.raw["new_field"]` without waiting for a client release.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    import pandas as pd


class DeltaLabAPIError(Exception):
    """Raised for typed error envelopes returned by the Delta Lab API.

    The server returns `{"error": "<code>", "message": "..."}` with a mapped
    HTTP status. This exception preserves the code so callers can branch on
    it (e.g. `not_found`, `bulk_cap_exceeded`, `invalid_*`) without parsing
    error text.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int,
        url: str | None = None,
    ) -> None:
        super().__init__(f"[{code}] {message}" + (f" ({url})" if url else ""))
        self.code = code
        self.message = message
        self.status = status
        self.url = url


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


@dataclass
class AssetInfo:
    asset_id: int
    symbol: str
    name: str | None = None
    decimals: int | None = None
    chain_id: int | None = None
    address: str | None = None
    coingecko_id: str | None = None
    source: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AssetInfo:
        return cls(
            asset_id=int(data["asset_id"]),
            symbol=data["symbol"],
            name=data.get("name"),
            decimals=data.get("decimals"),
            chain_id=data.get("chain_id"),
            address=data.get("address"),
            coingecko_id=data.get("coingecko_id"),
            source=data.get("source"),
            raw=data,
        )


@dataclass
class VenueInfo:
    venue_id: int
    name: str
    venue_type: str | None = None
    chain_id: int | None = None
    market_count: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VenueInfo:
        return cls(
            venue_id=int(data["venue_id"]),
            name=data["name"],
            venue_type=data.get("venue_type"),
            chain_id=data.get("chain_id"),
            market_count=data.get("market_count"),
            extra=data.get("extra") or {},
            raw=data,
        )


@dataclass
class MarketInfo:
    market_id: int
    venue: str | None = None
    venue_id: int | None = None
    venue_type: str | None = None
    market_type: str | None = None
    external_id: str | None = None
    chain_id: int | None = None
    is_listed: bool | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MarketInfo:
        return cls(
            market_id=int(data["market_id"]),
            venue=data.get("venue"),
            venue_id=data.get("venue_id"),
            venue_type=data.get("venue_type"),
            market_type=data.get("market_type"),
            external_id=data.get("external_id"),
            chain_id=data.get("chain_id"),
            is_listed=data.get("is_listed"),
            extra=data.get("extra") or {},
            raw=data,
        )


@dataclass
class InstrumentInfo:
    instrument_id: int | None = None
    venue: str | None = None
    chain_id: int | None = None
    market_id: int | None = None
    base_symbol: str | None = None
    base_asset_id: int | None = None
    quote_asset_id: int | None = None
    maturity_ts: datetime | None = None
    instrument_type: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InstrumentInfo:
        # Spec uses `instrument_id` at response root or inside opportunity payloads;
        # individual instrument lookups may only expose `market_id`.
        return cls(
            instrument_id=data.get("instrument_id") or data.get("id"),
            venue=data.get("venue"),
            chain_id=data.get("chain_id"),
            market_id=data.get("market_id"),
            base_symbol=data.get("base_symbol"),
            base_asset_id=data.get("base_asset_id"),
            quote_asset_id=data.get("quote_asset_id"),
            maturity_ts=_parse_ts(data.get("maturity_ts")),
            instrument_type=data.get("instrument_type"),
            extra=data.get("extra") or {},
            raw=data,
        )


@dataclass
class PriceLatest:
    """Point-in-time price snapshot + return/vol/mdd stats.

    `price/latest/` merges the current price and pre-computed statistics into
    a single payload. Typical use: screening / ranking without pulling TS.
    """

    asset_id: int
    asof_ts: datetime
    price_usd: float | None = None
    ret_1d: float | None = None
    ret_7d: float | None = None
    ret_30d: float | None = None
    ret_90d: float | None = None
    vol_7d: float | None = None
    vol_30d: float | None = None
    vol_90d: float | None = None
    mdd_30d: float | None = None
    mdd_90d: float | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    _FIELDS: ClassVar[tuple[str, ...]] = (
        "price_usd",
        "ret_1d",
        "ret_7d",
        "ret_30d",
        "ret_90d",
        "vol_7d",
        "vol_30d",
        "vol_90d",
        "mdd_30d",
        "mdd_90d",
    )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PriceLatest:
        return cls(
            asset_id=int(data["asset_id"]),
            asof_ts=_parse_ts(data["asof_ts"]) or datetime.min,
            **{k: data.get(k) for k in cls._FIELDS},
            raw=data,
        )


@dataclass
class YieldLatest:
    asset_id: int
    asof_ts: datetime
    apy_base: float | None = None
    apy_base_7d: float | None = None
    tvl_usd: float | None = None
    exchange_rate: float | None = None
    yield_token_symbol: str | None = None
    yield_token_asset_id: int | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> YieldLatest:
        return cls(
            asset_id=int(data.get("asset_id") or data.get("underlying_asset_id")),
            asof_ts=_parse_ts(data.get("asof_ts") or data.get("ts")) or datetime.min,
            apy_base=data.get("apy_base"),
            apy_base_7d=data.get("apy_base_7d"),
            tvl_usd=data.get("tvl_usd"),
            exchange_rate=data.get("exchange_rate"),
            yield_token_symbol=data.get("yield_token_symbol"),
            yield_token_asset_id=data.get("yield_token_asset_id"),
            raw=data,
        )


@dataclass
class LendingLatest:
    """Rich lending snapshot: current values + 7d/30d means/stds/z-scores.

    The payload is ~50 fields; `raw` keeps them all. The attributes below are
    the commonly-used subset — reach into `raw` for e.g. combined_* variants.
    """

    market_id: int
    asset_id: int
    asof_ts: datetime
    venue_id: int | None = None
    venue_name: str | None = None
    market_label: str | None = None
    asset_price_usd: float | None = None
    net_supply_apr_now: float | None = None
    net_borrow_apr_now: float | None = None
    supply_tvl_usd: float | None = None
    borrow_tvl_usd: float | None = None
    liquidity_usd: float | None = None
    util_now: float | None = None
    util_mean_30d: float | None = None
    util_z_30d: float | None = None
    ltv_max: float | None = None
    liq_threshold: float | None = None
    liquidation_penalty: float | None = None
    is_frozen: bool | None = None
    is_paused: bool | None = None
    is_collateral_enabled: bool | None = None
    borrow_spike_score: float | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LendingLatest:
        return cls(
            market_id=int(data["market_id"]),
            asset_id=int(data["asset_id"]),
            asof_ts=_parse_ts(data["asof_ts"]) or datetime.min,
            venue_id=data.get("venue_id"),
            venue_name=data.get("venue_name"),
            market_label=data.get("market_label"),
            asset_price_usd=data.get("asset_price_usd"),
            net_supply_apr_now=data.get("net_supply_apr_now"),
            net_borrow_apr_now=data.get("net_borrow_apr_now"),
            supply_tvl_usd=data.get("supply_tvl_usd"),
            borrow_tvl_usd=data.get("borrow_tvl_usd"),
            liquidity_usd=data.get("liquidity_usd"),
            util_now=data.get("util_now"),
            util_mean_30d=data.get("util_mean_30d"),
            util_z_30d=data.get("util_z_30d"),
            ltv_max=data.get("ltv_max"),
            liq_threshold=data.get("liq_threshold"),
            liquidation_penalty=data.get("liquidation_penalty"),
            is_frozen=data.get("is_frozen"),
            is_paused=data.get("is_paused"),
            is_collateral_enabled=data.get("is_collateral_enabled"),
            borrow_spike_score=data.get("borrow_spike_score"),
            raw=data,
        )


@dataclass
class BorosLatest:
    market_id: int
    asof_ts: datetime
    pv: float | None = None
    fixed_rate_mark: float | None = None
    floating_rate_oracle: float | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BorosLatest:
        return cls(
            market_id=int(data["market_id"]),
            asof_ts=_parse_ts(data["asof_ts"]) or datetime.min,
            pv=data.get("pv"),
            fixed_rate_mark=data.get("fixed_rate_mark"),
            floating_rate_oracle=data.get("floating_rate_oracle"),
            raw=data,
        )


@dataclass
class PendleLatest:
    market_id: int
    asof_ts: datetime
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PendleLatest:
        return cls(
            market_id=int(data["market_id"]),
            asof_ts=_parse_ts(data["asof_ts"]) or datetime.min,
            raw=data,
        )


@dataclass
class FundingLatest:
    instrument_id: int
    asof_ts: datetime
    venue: str | None = None
    funding_rate: float | None = None
    mark_price_usd: float | None = None
    oi_usd: float | None = None
    volume_usd: float | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FundingLatest:
        return cls(
            instrument_id=int(data["instrument_id"]),
            asof_ts=_parse_ts(data.get("asof_ts") or data.get("ts")) or datetime.min,
            venue=data.get("venue"),
            funding_rate=data.get("funding_rate"),
            mark_price_usd=data.get("mark_price_usd"),
            oi_usd=data.get("oi_usd"),
            volume_usd=data.get("volume_usd"),
            raw=data,
        )


@dataclass
class BacktestBundle:
    """Merged response from `POST /backtest/fetch/`.

    Hydrates everything needed to run a per-opportunity backtest in a single
    call: the discovery-shape opportunity list plus per-instrument funding TS
    and per-(market, asset) lending TS.
    """

    basis_root: str
    side: str | None
    lookback_days: int
    start: datetime | None
    end: datetime | None
    opportunities: pd.DataFrame
    lending_ts: dict[tuple[int, int], pd.DataFrame]
    funding_ts: dict[int, pd.DataFrame]
    raw: dict[str, Any] = field(default_factory=dict, repr=False)
