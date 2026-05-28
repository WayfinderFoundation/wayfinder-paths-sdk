from __future__ import annotations

import logging
import math
from typing import Any

from wayfinder_paths.core.clients.DeltaLabClient import DELTA_LAB_CLIENT
from wayfinder_paths.core.constants.chains import CHAIN_CODE_TO_ID
from wayfinder_paths.mcp.arg_validation import (
    MCPArgumentError,
    normalize_int,
    optional_int,
    optional_str,
)
from wayfinder_paths.mcp.utils import catch_errors, ok

logger = logging.getLogger(__name__)

_SKIP_VALUES = {"", "_", "all", "none", "null"}
_INSTRUMENT_TYPE_ALIASES = {
    "PT": "PENDLE_PT",
}
_KNOWN_INSTRUMENT_TYPES = {
    "PERP",
    "LENDING_SUPPLY",
    "LENDING_BORROW",
    "BOROS_MARKET",
    "PENDLE_PT",
    "YIELD_TOKEN",
}


def _optional_text(value: str | int) -> str | None:
    return optional_str(value, skip_values=_SKIP_VALUES, max_length=None)


def _chain_filter(value: str | int, *, field_name: str = "chain") -> int | None:
    normalized = _optional_text(value)
    if normalized is None:
        return None
    if normalized.isdigit():
        return int(normalized)
    chain_id = CHAIN_CODE_TO_ID.get(normalized.lower())
    if chain_id is None:
        raise MCPArgumentError(
            f"unknown chain filter: {value!r}",
            field=field_name,
            received=value,
            allowed_values=[
                *CHAIN_CODE_TO_ID.keys(),
                *map(str, CHAIN_CODE_TO_ID.values()),
            ],
        )
    return chain_id


def _instrument_type_filter(value: str) -> str | None:
    normalized = _optional_text(value)
    if normalized is None:
        return None
    upper = normalized.upper()
    if upper in _INSTRUMENT_TYPE_ALIASES:
        return _INSTRUMENT_TYPE_ALIASES[upper]
    if upper in _KNOWN_INSTRUMENT_TYPES:
        return upper
    return normalized


def _json_safe(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def _df_records(df) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    frame = df.reset_index()
    return [
        {key: _json_safe(value) for key, value in row.items()}
        for row in frame.to_dict(orient="records")
    ]


async def _resolve_basis_filter(
    symbol: str,
) -> tuple[str | None, list[int] | None]:
    """Resolve an asset symbol into Delta Lab screen filters."""
    try:
        result = await DELTA_LAB_CLIENT.get_asset_basis(symbol=symbol)
    except Exception as exc:
        raise ValueError(
            f"Unknown Delta Lab asset symbol {symbol!r}; "
            "check the spelling or call research_get_basis_symbols / "
            "research_search_delta_lab_assets to discover valid symbols."
        ) from exc
    basis = result.get("basis")
    if basis and basis.get("root_symbol"):
        root = basis["root_symbol"]
        if root != symbol:
            logger.debug("Resolved basis symbol %s -> %s", symbol, root)
        return root, None
    asset_id = result.get("asset_id")
    if isinstance(asset_id, int):
        logger.debug(
            "Asset %s has no basis group; falling back to asset_ids=[%d]",
            symbol,
            asset_id,
        )
        return None, [asset_id]
    raise ValueError(
        f"Symbol {symbol!r} resolved without a basis group or asset_id; "
        "cannot apply a filter."
    )


async def _screen_basis_filter(value: str) -> tuple[str | None, list[int] | None]:
    basis = _optional_text(value)
    if basis is None:
        return None, None
    return await _resolve_basis_filter(basis.upper())


async def _resolve_basis_root(symbol: str) -> str:
    """Resolve a symbol to its basis root, falling back to the input unchanged.

    Used by endpoints that only accept a basis symbol (no asset_ids escape
    hatch) — callers must accept that an unresolved symbol gets forwarded.
    """
    try:
        root, _ = await _resolve_basis_filter(symbol)
    except ValueError:
        return symbol
    return root or symbol


@catch_errors
async def research_get_basis_apy_sources(
    basis_symbol: str, lookback_days: str | int = "7", limit: str | int = "25"
) -> dict[str, Any]:
    """Get top yield opportunities for a given asset across protocols.

    Args:
        basis_symbol: Root symbol (e.g., "BTC", "ETH", "HYPE")
        lookback_days: Days to look back for averaging (default: "7", min: "1")
        limit: Max opportunities to return (default: "25", max: "1000")

    Returns:
        Dict with basis info, opportunities grouped by LONG/SHORT, summary stats
    """
    lookback_int = normalize_int(lookback_days, field_name="lookback_days", min_value=1)
    limit_int = min(1000, normalize_int(limit, field_name="limit", min_value=1))
    resolved = await _resolve_basis_root(basis_symbol.upper())
    return ok(
        await DELTA_LAB_CLIENT.get_basis_apy_sources(
            basis_symbol=resolved,
            lookback_days=lookback_int,
            limit=limit_int,
        )
    )


@catch_errors
async def research_get_basis_symbols() -> dict[str, Any]:
    """Get list of available basis symbols.

    Returns all available basis symbols in Delta Lab.

    Returns:
        Dict with symbols list and total count
    """
    return ok(await DELTA_LAB_CLIENT.get_basis_symbols(get_all=True))


@catch_errors
async def research_get_asset_basis_info(symbol: str) -> dict[str, Any]:
    """Get basis group information for an asset.

    Args:
        symbol: Asset symbol (e.g., "ETH", "BTC")

    Returns:
        Dict with asset_id, symbol, and basis group information
    """
    return ok(await DELTA_LAB_CLIENT.get_asset_basis(symbol=symbol.upper()))


@catch_errors
async def research_search_delta_lab_assets(
    query: str, chain: str | int = "all", limit: str | int = "25"
) -> dict[str, Any]:
    """Search Delta Lab assets by symbol/name/address/coingecko_id.

    Args:
        query: Search term (symbol, name, address, coingecko_id, or numeric asset_id)
        chain: Optional chain filter (chain ID like "8453" or chain code like "base").
               Use "all" for no filter.
        limit: Max results (default: "25", max: "200")

    Returns:
        Dict with "assets" list and "total_count"
    """
    return ok(
        await DELTA_LAB_CLIENT.search_assets(
            query=query.strip(),
            chain_id=_chain_filter(chain),
            limit=min(200, normalize_int(limit, field_name="limit", min_value=1)),
        )
    )


@catch_errors
async def research_search_delta_lab_markets(
    venue: str = "all",
    chain: str | int = "all",
    marketType: str = "all",
    assetId: str | int = "_",
    basisRoot: str = "all",
    limit: str | int = "25",
    offset: str | int = "0",
) -> dict[str, Any]:
    """Search Delta Lab markets by venue, chain, type, asset id, or basis root.

    Chain accepts "all", canonical chain codes, or numeric chain IDs as strings:
    "arbitrum"/"42161", "base"/"8453", "plasma"/"9745", "sonic"/"146",
    "ethereum"/"1", "hyperevm"/"999", and "bsc"/"56".

    For Pendle stablecoin/PT yield ranking, prefer
    `research_search_delta_lab_instruments(venue="pendle", basisRoot="USD", ...)`
    first. Pendle market search can return sparse market IDs and is better
    after instrument/basis discovery or for all-market volume analysis.
    """
    return ok(
        await DELTA_LAB_CLIENT.search_markets(
            venue=_optional_text(venue),
            chain_id=_chain_filter(chain),
            market_type=_optional_text(marketType),
            asset_id=optional_int(assetId, field_name="assetId"),
            basis_root=_optional_text(basisRoot.upper()),
            limit=min(100, normalize_int(limit, field_name="limit", min_value=1)),
            offset=normalize_int(offset, field_name="offset", min_value=0),
        )
    )


@catch_errors
async def research_search_delta_lab_instruments(
    instrumentType: str = "all",
    basisRoot: str = "all",
    venue: str = "all",
    chain: str | int = "all",
    quoteAssetId: str | int = "_",
    maturityAfter: str = "_",
    maturityBefore: str = "_",
    limit: str | int = "25",
    offset: str | int = "0",
) -> dict[str, Any]:
    """Search Delta Lab instruments, including Pendle PT instruments.

    Chain accepts "all", canonical chain codes, or numeric chain IDs as strings:
    "arbitrum"/"42161", "base"/"8453", "plasma"/"9745", "sonic"/"146",
    "ethereum"/"1", "hyperevm"/"999", and "bsc"/"56".

    For Pendle stablecoin yields, use `venue="pendle"`, the target `chain`,
    and `basisRoot="USD"` first. Delta Lab models Pendle PTs as
    `instrumentType="PENDLE_PT"`; bare `"PT"` is accepted as an alias.
    Do not use bare `"YT"` unless backend docs or returned rows confirm a
    matching instrument enum for the environment.
    """
    return ok(
        await DELTA_LAB_CLIENT.search_instruments(
            instrument_type=_instrument_type_filter(instrumentType),
            basis_root=_optional_text(basisRoot.upper()),
            venue=_optional_text(venue),
            chain_id=_chain_filter(chain),
            quote_asset_id=optional_int(quoteAssetId, field_name="quoteAssetId"),
            maturity_after=_optional_text(maturityAfter),
            maturity_before=_optional_text(maturityBefore),
            limit=min(100, normalize_int(limit, field_name="limit", min_value=1)),
            offset=normalize_int(offset, field_name="offset", min_value=0),
        )
    )


@catch_errors
async def research_get_delta_lab_pendle_market(
    marketID: str | int,
    lookbackDays: str | int = "30",
    limit: str | int = "500",
) -> dict[str, Any]:
    """Get latest and time-series Delta Lab Pendle analytics for one market."""
    market_id = normalize_int(marketID, field_name="marketID", min_value=1)
    lookback_days = normalize_int(lookbackDays, field_name="lookbackDays", min_value=1)
    limit_int = min(5000, normalize_int(limit, field_name="limit", min_value=1))
    latest = await DELTA_LAB_CLIENT.get_market_pendle_latest(market_id=market_id)
    ts = await DELTA_LAB_CLIENT.get_market_pendle_ts(
        market_id=market_id,
        lookback_days=lookback_days,
        limit=limit_int,
    )
    return ok(
        {
            "marketID": market_id,
            "latest": latest.raw if latest else None,
            "rows": _df_records(ts),
            "count": 0 if ts is None else len(ts),
            "lookbackDays": lookback_days,
        }
    )


@catch_errors
async def research_get_top_apy(
    lookback_days: str | int = "7",
    limit: str | int = "25",
    instrument_type: str | None = None,
) -> dict[str, Any]:
    """Get top APY opportunities across all basis symbols.

    Without `instrument_type` the leaderboard is dominated by Aerodrome
    Slipstream LPs (YIELD_TOKEN) with projected fee APRs in the hundreds of
    percent — pass an instrument type for a useful per-category view.

    Args:
        lookback_days: Days to average over (default: "7", min: "1")
        limit: Max opportunities to return (default: "25", max: "500")
        instrument_type: Optional filter. One of "perp", "pendle_pt",
            "boros_market", "boros_vault", "yield_token", "lending_supply".
    """
    lookback_int = normalize_int(lookback_days, field_name="lookback_days", min_value=1)
    limit_int = min(500, normalize_int(limit, field_name="limit", min_value=1))
    return ok(
        await DELTA_LAB_CLIENT.get_top_apy(
            lookback_days=lookback_int,
            limit=limit_int,
            instrument_type=instrument_type,
        )
    )


@catch_errors
async def research_search_price(
    sort: str = "price_usd",
    limit: str | int = "25",
    basis: str = "all",
) -> dict[str, Any]:
    """Screen assets by price features (returns, volatility, drawdowns).

    Args:
        sort: Column to sort by (default: "price_usd"). Options include:
              price_usd, ret_1d, ret_7d, ret_30d, ret_90d,
              vol_7d, vol_30d, vol_90d, mdd_30d, mdd_90d
        limit: Max rows to return (default: "25", max: "1000"). Prefer the
              default for exploratory scans; raise only after narrowing by
              `basis` or another filter.
        basis: Basis symbol or asset symbol to filter by (e.g. "ETH", "USDC").
               Asset symbols are auto-resolved to their root basis (USDC -> USD).
               Use "all" for no filter.

    Returns:
        Dict with data (list of price feature rows) and count
    """
    limit_int = min(1000, normalize_int(limit, field_name="limit", min_value=1))
    basis_param, asset_ids_param = await _screen_basis_filter(basis)
    return ok(
        await DELTA_LAB_CLIENT.screen_price(
            sort=sort.strip(),
            limit=limit_int,
            basis=basis_param,
            asset_ids=asset_ids_param,
        )
    )


@catch_errors
async def research_search_lending(
    sort: str = "net_supply_apr_now",
    limit: str | int = "25",
    basis: str = "all",
) -> dict[str, Any]:
    """Screen lending markets by surface features (supply/borrow APRs, TVL).

    Args:
        sort: Column to sort by (default: "net_supply_apr_now"). Options include:
              net_supply_apr_now, net_supply_mean_7d, net_supply_mean_30d,
              combined_net_supply_apr_now, net_borrow_apr_now,
              supply_tvl_usd, liquidity_usd, util_now, borrow_spike_score
        limit: Max rows to return (default: "25", max: "1000"). Prefer the
              default for exploratory scans; raise only after narrowing by
              `basis` or another filter.
        basis: Basis symbol or asset symbol to filter by (e.g. "ETH", "USDC").
               Asset symbols are auto-resolved to their root basis (USDC -> USD).
               Use "all" for no filter.

    Returns:
        Dict with data (list of lending surface feature rows) and count
    """
    limit_int = min(1000, normalize_int(limit, field_name="limit", min_value=1))
    basis_param, asset_ids_param = await _screen_basis_filter(basis)
    return ok(
        await DELTA_LAB_CLIENT.screen_lending(
            sort=sort.strip(),
            limit=limit_int,
            basis=basis_param,
            asset_ids=asset_ids_param,
            exclude_frozen=True,
        )
    )


@catch_errors
async def research_search_perp(
    sort: str = "funding_now",
    limit: str | int = "25",
    basis: str = "all",
) -> dict[str, Any]:
    """Screen perpetual markets by surface features (funding, basis, OI).

    Args:
        sort: Column to sort by (default: "funding_now"). Options include:
              funding_now, funding_mean_7d, funding_mean_30d,
              basis_now, basis_mean_7d, basis_mean_30d,
              oi_now, volume_24h, mark_price
        limit: Max rows to return (default: "25", max: "1000"). Prefer the
              default for exploratory scans; raise only after narrowing by
              `basis` or another filter.
        basis: Basis symbol or asset symbol to filter by (e.g. "ETH", "USDC").
               Asset symbols are auto-resolved to their root basis (USDC -> USD).
               Use "all" for no filter.

    Returns:
        Dict with data (list of perp surface feature rows) and count
    """
    limit_int = min(1000, normalize_int(limit, field_name="limit", min_value=1))
    basis_param, asset_ids_param = await _screen_basis_filter(basis)
    return ok(
        await DELTA_LAB_CLIENT.screen_perp(
            sort=sort.strip(),
            limit=limit_int,
            basis=basis_param,
            asset_ids=asset_ids_param,
        )
    )


@catch_errors
async def research_search_borrow_routes(
    sort: str = "ltv_max",
    limit: str | int = "25",
    basis: str = "all",
    borrow_basis: str = "all",
    chain_id: str | int = "all",
) -> dict[str, Any]:
    """Screen borrow routes (collateral → borrow) by route configuration.

    Args:
        sort: Column to sort by (default: "ltv_max"). Options include:
              ltv_max, liq_threshold, liquidation_penalty, debt_ceiling_usd,
              venue_name, market_label, created_at
        limit: Max rows to return (default: "25", max: "1000"). Prefer the
              default for exploratory scans; raise only after narrowing by
              `basis`, `borrow_basis`, or `chain_id`.
        basis: Collateral basis symbol to filter by (e.g. "ETH"). Use "all" for no filter.
        borrow_basis: Borrow basis symbol to filter by (e.g. "USD"). Use "all" for no filter.
        chain_id: Optional chain filter (chain ID like "8453" or chain code like "base").
                 Use "all" for no filter.

    Returns:
        Dict with data (list of borrow route rows) and count
    """
    limit_int = min(1000, normalize_int(limit, field_name="limit", min_value=1))
    basis_param, asset_ids_param = await _screen_basis_filter(basis)
    borrow_basis_param, borrow_asset_ids_param = await _screen_basis_filter(
        borrow_basis
    )
    return ok(
        await DELTA_LAB_CLIENT.screen_borrow_routes(
            sort=sort.strip(),
            limit=limit_int,
            basis=basis_param,
            asset_ids=asset_ids_param,
            borrow_basis=borrow_basis_param,
            borrow_asset_ids=borrow_asset_ids_param,
            chain_id=_chain_filter(chain_id, field_name="chain_id"),
        )
    )
