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
from wayfinder_paths.mcp.tool_annotations import ChainFilter, DeltaInstrumentType
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
    """Rank cross-protocol yield opportunities for an asset basis.

    Use an asset/root symbol such as BTC, ETH, or HYPE; symbols are uppercased
    and resolved to their basis root. Results group LONG/SHORT opportunities.
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
    """List every basis symbol currently available in Delta Lab."""
    return ok(await DELTA_LAB_CLIENT.get_basis_symbols(get_all=True))


@catch_errors
async def research_get_asset_basis_info(symbol: str) -> dict[str, Any]:
    """Resolve an asset symbol to its Delta Lab asset and basis group."""
    return ok(await DELTA_LAB_CLIENT.get_asset_basis(symbol=symbol.upper()))


@catch_errors
async def research_search_delta_lab_assets(
    query: str, chain: ChainFilter = "all", limit: str | int = "25"
) -> dict[str, Any]:
    """Search Delta Lab assets by symbol, name, address, CoinGecko id, or asset id.

    `chain` accepts a canonical code, numeric chain id, or "all"; limit caps at 200.
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
    chain: ChainFilter = "all",
    marketType: str = "all",
    assetId: str | int = "_",
    basisRoot: str = "all",
    limit: str | int = "25",
    offset: str | int = "0",
) -> dict[str, Any]:
    """Search Delta Lab markets by venue, chain, type, asset id, or basis root.

    `chain` accepts a code, numeric id, or "all". For Pendle PT/yield discovery,
    search instruments first; market rows can be sparse and are better for
    follow-up hydration or volume analysis.
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
    instrumentType: DeltaInstrumentType = "all",
    basisRoot: str = "all",
    venue: str = "all",
    chain: ChainFilter = "all",
    quoteAssetId: str | int = "_",
    maturityAfter: str = "_",
    maturityBefore: str = "_",
    limit: str | int = "25",
    offset: str | int = "0",
) -> dict[str, Any]:
    """Search Delta Lab instruments, including Pendle PT instruments.

    `chain` accepts a code, numeric id, or "all". For Pendle stablecoin yields,
    start with venue=pendle and basisRoot=USD. PT aliases to PENDLE_PT; do not
    assume YT is a supported instrument enum.
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
    """Rank APY opportunities across all basis symbols.

    Prefer `instrument_type` for useful category comparisons; unfiltered output
    is often dominated by high projected-fee YIELD_TOKEN LPs.
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
    """Screen price, return, volatility, or drawdown features.

    Sort with price_usd, ret_{1,7,30,90}d, vol_{7,30,90}d, or
    mdd_{30,90}d. `basis` accepts an asset/root symbol or "all" and is
    auto-resolved (for example USDC to USD). Keep broad scans near limit=25.
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
    """Screen non-frozen lending markets by APR, TVL, liquidity, or utilization.

    Useful sorts include net_supply_apr_now, net_borrow_apr_now,
    supply_tvl_usd, liquidity_usd, util_now, and borrow_spike_score.
    `basis` accepts an asset/root symbol or "all"; keep broad scans near limit=25.
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
    """Screen perpetuals by funding, basis, open interest, volume, or mark price.

    Sort with funding/basis `_now` or `_mean_{7,30}d`, oi_now, volume_24h,
    or mark_price. `basis` accepts an asset/root symbol or "all"; keep broad
    scans near limit=25.
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
    chain_id: ChainFilter = "all",
) -> dict[str, Any]:
    """Screen collateral-to-borrow routes by LTV and liquidation configuration.

    `basis` is collateral; `borrow_basis` is debt. Both accept asset/root
    symbols or "all"; `chain_id` also accepts a chain code. Useful sorts include
    ltv_max, liq_threshold, liquidation_penalty, and debt_ceiling_usd.
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
