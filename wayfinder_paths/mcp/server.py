"""Wayfinder Paths MCP server (FastMCP).

Run locally (via Claude Code .mcp.json):
  poetry run python -m wayfinder_paths.mcp.server
"""

from __future__ import annotations

import asyncio

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ResourceError

from wayfinder_paths.core.config import is_opencode_instance
from wayfinder_paths.mcp.resources.alpha_lab import get_alpha_types, search_alpha
from wayfinder_paths.mcp.resources.contracts import (
    get_contract,
    list_contracts,
)
from wayfinder_paths.mcp.resources.delta_lab import (
    get_asset_basis_info,
    get_asset_timeseries_data,
    get_asset_timeseries_with_venue,
    get_assets_by_address,
    get_basis_apy_sources,
    get_basis_symbols,
    get_basis_timeseries_data,
    get_basis_timeseries_with_venue,
    get_best_delta_neutral_pairs,
    get_delta_lab_asset,
    get_top_apy,
    screen_borrow_routes,
    screen_borrow_routes_by_asset_ids,
    screen_lending,
    screen_lending_by_asset_ids,
    screen_perp,
    screen_perp_by_asset_ids,
    screen_price,
    screen_price_by_asset_ids,
    search_delta_lab_assets,
)
from wayfinder_paths.mcp.resources.discovery import (
    describe_adapter,
    describe_strategy,
    list_adapters,
    list_strategies,
)
from wayfinder_paths.mcp.resources.hyperliquid import (
    get_markets,
    get_mid_price,
    get_mid_prices,
    get_orderbook,
    get_spot_assets,
    get_spot_user_state,
    get_user_state,
)
from wayfinder_paths.mcp.resources.tokens import (
    fuzzy_search_tokens,
    get_gas_token,
    resolve_token,
)
from wayfinder_paths.mcp.resources.wallets import (
    get_wallet,
    get_wallet_activity,
    get_wallet_balances,
    list_wallets,
)
from wayfinder_paths.mcp.tools.contracts import compile_contract, deploy_contract
from wayfinder_paths.mcp.tools.evm_contract import (
    contract_call,
    contract_execute,
    contract_get_abi,
)
from wayfinder_paths.mcp.tools.execute import execute
from wayfinder_paths.mcp.tools.hyperliquid import hyperliquid, hyperliquid_execute
from wayfinder_paths.mcp.tools.instance_state import (
    add_chart_projection,
    clear_chart_projections,
    get_frontend_context,
    remove_chart_projection,
)
from wayfinder_paths.mcp.tools.notify import notify
from wayfinder_paths.mcp.tools.polymarket import polymarket, polymarket_execute
from wayfinder_paths.mcp.tools.quotes import quote_swap
from wayfinder_paths.mcp.tools.run_script import run_script
from wayfinder_paths.mcp.tools.runner import runner
from wayfinder_paths.mcp.tools.strategies import run_strategy
from wayfinder_paths.mcp.tools.wallets import wallets
from wayfinder_paths.mcp.utils import err, ok
from wayfinder_paths.paths.heartbeat import maybe_heartbeat_installed_paths

mcp = FastMCP("wayfinder")

# Resources (read-only data)
mcp.resource("wayfinder://adapters")(list_adapters)
mcp.resource("wayfinder://strategies")(list_strategies)
mcp.resource("wayfinder://adapters/{name}")(describe_adapter)
mcp.resource("wayfinder://strategies/{name}")(describe_strategy)
mcp.resource("wayfinder://wallets")(list_wallets)
mcp.resource("wayfinder://wallets/{label}")(get_wallet)
mcp.resource("wayfinder://balances/{label}")(get_wallet_balances)
mcp.resource("wayfinder://activity/{label}")(get_wallet_activity)
mcp.resource("wayfinder://tokens/resolve/{query}")(resolve_token)
mcp.resource("wayfinder://tokens/gas/{chain_code}")(get_gas_token)
mcp.resource("wayfinder://tokens/search/{chain_code}/{query}")(fuzzy_search_tokens)
mcp.resource("wayfinder://hyperliquid/{label}/state")(get_user_state)
mcp.resource("wayfinder://hyperliquid/{label}/spot")(get_spot_user_state)
mcp.resource("wayfinder://hyperliquid/prices")(get_mid_prices)
mcp.resource("wayfinder://hyperliquid/prices/{coin}")(get_mid_price)
mcp.resource("wayfinder://hyperliquid/markets")(get_markets)
mcp.resource("wayfinder://hyperliquid/spot-assets")(get_spot_assets)
mcp.resource("wayfinder://hyperliquid/book/{coin}")(get_orderbook)
mcp.resource("wayfinder://contracts")(list_contracts)
mcp.resource("wayfinder://contracts/{chain_id}/{address}")(get_contract)
mcp.resource("wayfinder://alpha-lab/types")(get_alpha_types)
mcp.resource(
    "wayfinder://alpha-lab/search/{query}/{scan_type}/{created_after}/{created_before}/{limit}"
)(search_alpha)
mcp.resource("wayfinder://delta-lab/symbols")(get_basis_symbols)
mcp.resource("wayfinder://delta-lab/top-apy/{lookback_days}/{limit}")(get_top_apy)
mcp.resource(
    "wayfinder://delta-lab/{basis_symbol}/apy-sources/{lookback_days}/{limit}"
)(get_basis_apy_sources)
mcp.resource(
    "wayfinder://delta-lab/{basis_symbol}/delta-neutral/{lookback_days}/{limit}"
)(get_best_delta_neutral_pairs)
mcp.resource("wayfinder://delta-lab/assets/{asset_id}")(get_delta_lab_asset)
mcp.resource("wayfinder://delta-lab/assets/by-address/{address}/{chain_id}")(
    get_assets_by_address
)
mcp.resource("wayfinder://delta-lab/assets/search/{chain}/{query}/{limit}")(
    search_delta_lab_assets
)
mcp.resource("wayfinder://delta-lab/{symbol}/basis")(get_asset_basis_info)
mcp.resource(
    "wayfinder://delta-lab/{symbol}/timeseries/{series}/{lookback_days}/{limit}"
)(get_asset_timeseries_data)
mcp.resource(
    "wayfinder://delta-lab/{symbol}/timeseries/{series}/{lookback_days}/{limit}/{venue}"
)(get_asset_timeseries_with_venue)
mcp.resource(
    "wayfinder://delta-lab/basis/{symbol}/timeseries/{series}/{lookback_days}/{limit}"
)(get_basis_timeseries_data)
mcp.resource(
    "wayfinder://delta-lab/basis/{symbol}/timeseries/{series}/{lookback_days}/{limit}/{venue}"
)(get_basis_timeseries_with_venue)
mcp.resource("wayfinder://delta-lab/screen/price/{sort}/{limit}/{basis}")(screen_price)
mcp.resource(
    "wayfinder://delta-lab/screen/price/by-asset-ids/{sort}/{limit}/{asset_ids}"
)(screen_price_by_asset_ids)
mcp.resource("wayfinder://delta-lab/screen/lending/{sort}/{limit}/{basis}")(
    screen_lending
)
mcp.resource(
    "wayfinder://delta-lab/screen/lending/by-asset-ids/{sort}/{limit}/{asset_ids}"
)(screen_lending_by_asset_ids)
mcp.resource("wayfinder://delta-lab/screen/perp/{sort}/{limit}/{basis}")(screen_perp)
mcp.resource(
    "wayfinder://delta-lab/screen/perp/by-asset-ids/{sort}/{limit}/{asset_ids}"
)(screen_perp_by_asset_ids)
mcp.resource(
    "wayfinder://delta-lab/screen/borrow-routes/{sort}/{limit}/{basis}/{borrow_basis}/{chain_id}"
)(screen_borrow_routes)
mcp.resource(
    "wayfinder://delta-lab/screen/borrow-routes/by-asset-ids/{sort}/{limit}/{asset_ids}/{borrow_asset_ids}/{chain_id}"
)(screen_borrow_routes_by_asset_ids)

# Tools (actions/mutations)
mcp.tool()(quote_swap)
mcp.tool()(hyperliquid)
mcp.tool()(hyperliquid_execute)
mcp.tool()(polymarket)
mcp.tool()(polymarket_execute)
mcp.tool()(run_strategy)
mcp.tool()(run_script)
mcp.tool()(execute)
mcp.tool()(wallets)
mcp.tool()(runner)
mcp.tool()(compile_contract)
mcp.tool()(deploy_contract)
mcp.tool()(contract_get_abi)
mcp.tool()(contract_call)
mcp.tool()(contract_execute)
mcp.tool()(notify)
mcp.tool()(get_frontend_context)
mcp.tool()(add_chart_projection)
mcp.tool()(remove_chart_projection)
mcp.tool()(clear_chart_projections)


async def read_resource(uri: str) -> dict:
    """Read an MCP resource by URI.

    Use for reads exposed as resources, e.g. `wayfinder://wallets`,
    `wayfinder://balances/main`, `wayfinder://strategies`,
    `wayfinder://delta-lab/top-apy/7/20`.
    """
    try:
        contents = list(await mcp.read_resource(uri))
    except (ResourceError, ValueError) as exc:
        return err("resource_read_failed", str(exc))

    parts: list[dict] = []
    for c in contents:
        text = (
            c.content
            if isinstance(c.content, str)
            else c.content.decode("utf-8", errors="replace")
        )
        parts.append({"mime_type": c.mime_type, "text": text})
    return ok({"uri": uri, "contents": parts})


if is_opencode_instance():
    mcp.tool()(read_resource)


def main() -> None:
    maybe_heartbeat_installed_paths(trigger="mcp-server")
    mcp.run()


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        if "asyncio.run()" in str(exc) and asyncio.get_event_loop().is_running():
            main()
        else:
            raise
