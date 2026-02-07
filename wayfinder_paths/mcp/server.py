"""Wayfinder Paths MCP server (FastMCP).

Run locally (via Claude Code .mcp.json):
  poetry run python -m wayfinder_paths.mcp.server
"""

from __future__ import annotations

import asyncio

from mcp.server.fastmcp import FastMCP

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
from wayfinder_paths.mcp.tools.execute import execute
from wayfinder_paths.mcp.tools.hyperliquid import hyperliquid, hyperliquid_execute
from wayfinder_paths.mcp.tools.quotes import quote_swap
from wayfinder_paths.mcp.tools.run_script import run_script
from wayfinder_paths.mcp.tools.runner import runner
from wayfinder_paths.mcp.tools.strategies import run_strategy
from wayfinder_paths.mcp.tools.wallets import wallets

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

# Tools (actions/mutations)
mcp.tool()(quote_swap)
mcp.tool()(hyperliquid)
mcp.tool()(hyperliquid_execute)
mcp.tool()(run_strategy)
mcp.tool()(run_script)
mcp.tool()(execute)
mcp.tool()(wallets)
mcp.tool()(runner)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        if "asyncio.run()" in str(exc) and asyncio.get_event_loop().is_running():
            main()
        else:
            raise
