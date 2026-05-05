"""Wayfinder Paths MCP server (FastMCP).

Run locally (via Claude Code .mcp.json):
  poetry run python -m wayfinder_paths.mcp.server

All MCP exports are registered as tools. Resources were nuked because opencode
does not auto-pull resources into model context; the agent only sees them via
the `read_resource` wrapper, which adds a redundant indirection. Plain tools
land in the model's tool spec on every turn.

Persona-scoped tools are namespaced as `{namespace}_{name}` so opencode's
per-agent `tools` allowlist can use one glob (`wayfinder_<namespace>_*: true`)
to scope a persona's surface. Cross-persona tools have no prefix — they're
expected to be allowlisted by every persona.

Namespaces:
  - shells       instance ↔ frontend bridge (chart projections, notify, ui ctx)
  - research     alpha-lab, delta-lab
  - hyperliquid  HL perp/spot/HIP-3/HIP-4 reads + writes
  - onchain      token resolution, swaps, wallet activity
  - polymarket   prediction markets reads + writes
  - contracts    contract compile/deploy/call/abi
  - (none)       shared cross-persona tools (discovery, wallets, run_script, …)
"""

from __future__ import annotations

import asyncio

from mcp.server.fastmcp import FastMCP

from wayfinder_paths.mcp.resources.alpha_lab import (
    research_get_alpha_types,
    research_search_alpha,
)
from wayfinder_paths.mcp.resources.contracts import contracts_get, contracts_list
from wayfinder_paths.mcp.resources.delta_lab import (
    research_get_asset_basis_info,
    research_get_basis_apy_sources,
    research_get_basis_symbols,
    research_get_top_apy,
    research_screen_borrow_routes,
    research_screen_lending,
    research_screen_perp,
    research_screen_price,
    research_search_delta_lab_assets,
)
from wayfinder_paths.mcp.resources.discovery import (
    describe_adapter,
    describe_strategy,
    list_adapters,
    list_strategies,
)
from wayfinder_paths.mcp.resources.hyperliquid import (
    hyperliquid_get_markets,
    hyperliquid_get_mid_price,
    hyperliquid_get_mid_prices,
    hyperliquid_get_orderbook,
    hyperliquid_get_outcome_user_state,
    hyperliquid_get_outcomes,
    hyperliquid_get_spot_assets,
    hyperliquid_get_spot_user_state,
    hyperliquid_get_user_state,
)
from wayfinder_paths.mcp.resources.tokens import (
    onchain_fuzzy_search_tokens,
    onchain_get_gas_token,
    onchain_resolve_token,
)
from wayfinder_paths.mcp.resources.wallets import (
    get_wallet,
    get_wallet_balances,
    onchain_get_wallet_activity,
    onchain_list_wallets,
)
from wayfinder_paths.mcp.tools.contracts import contracts_compile, contracts_deploy
from wayfinder_paths.mcp.tools.evm_contract import (
    contracts_call,
    contracts_execute,
    contracts_get_abi,
)
from wayfinder_paths.mcp.tools.execute import execute
from wayfinder_paths.mcp.tools.hyperliquid import hyperliquid_execute, hyperliquid_wait
from wayfinder_paths.mcp.tools.instance_state import (
    shells_add_chart_projection,
    shells_clear_chart_projections,
    shells_get_frontend_context,
    shells_remove_chart_projection,
)
from wayfinder_paths.mcp.tools.notify import shells_notify
from wayfinder_paths.mcp.tools.polymarket import polymarket_execute, polymarket_read
from wayfinder_paths.mcp.tools.quotes import onchain_quote_swap
from wayfinder_paths.mcp.tools.run_script import run_script
from wayfinder_paths.mcp.tools.runner import runner
from wayfinder_paths.mcp.tools.strategies import research_run_strategy
from wayfinder_paths.mcp.tools.wallets import wallets
from wayfinder_paths.paths.heartbeat import maybe_heartbeat_installed_paths

mcp = FastMCP("wayfinder")

# ─── shells_* ──────────────────────────────────────────────────────────
mcp.tool()(shells_get_frontend_context)
mcp.tool()(shells_add_chart_projection)
mcp.tool()(shells_remove_chart_projection)
mcp.tool()(shells_clear_chart_projections)
mcp.tool()(shells_notify)

# ─── research_* ────────────────────────────────────────────────────────
# Bulk / time-series delta-lab lives in DELTA_LAB_CLIENT (Python), not MCP —
# see the /using-delta-lab skill.
mcp.tool()(research_get_alpha_types)
mcp.tool()(research_search_alpha)
mcp.tool()(research_get_basis_symbols)
mcp.tool()(research_get_basis_apy_sources)
mcp.tool()(research_get_top_apy)
mcp.tool()(research_get_asset_basis_info)
mcp.tool()(research_search_delta_lab_assets)
mcp.tool()(research_screen_price)
mcp.tool()(research_screen_lending)
mcp.tool()(research_screen_perp)
mcp.tool()(research_screen_borrow_routes)
mcp.tool()(research_run_strategy)

# ─── hyperliquid_* ─────────────────────────────────────────────────────
# Coin naming reference: /using-hyperliquid-adapter/rules/coin-naming.md.
mcp.tool()(hyperliquid_wait)
mcp.tool()(hyperliquid_execute)
mcp.tool()(hyperliquid_get_user_state)
mcp.tool()(hyperliquid_get_spot_user_state)
mcp.tool()(hyperliquid_get_outcome_user_state)
mcp.tool()(hyperliquid_get_mid_prices)
mcp.tool()(hyperliquid_get_mid_price)
mcp.tool()(hyperliquid_get_markets)
mcp.tool()(hyperliquid_get_spot_assets)
mcp.tool()(hyperliquid_get_orderbook)
mcp.tool()(hyperliquid_get_outcomes)

# ─── onchain_* ─────────────────────────────────────────────────────────
mcp.tool()(onchain_resolve_token)
mcp.tool()(onchain_get_gas_token)
mcp.tool()(onchain_fuzzy_search_tokens)
mcp.tool()(onchain_list_wallets)
mcp.tool()(onchain_get_wallet_activity)
mcp.tool()(onchain_quote_swap)

# ─── polymarket_* ──────────────────────────────────────────────────────
mcp.tool()(polymarket_read)
mcp.tool()(polymarket_execute)

# ─── contracts_* ───────────────────────────────────────────────────────
mcp.tool()(contracts_list)
mcp.tool()(contracts_get)
mcp.tool()(contracts_compile)
mcp.tool()(contracts_deploy)
mcp.tool()(contracts_get_abi)
mcp.tool()(contracts_call)
mcp.tool()(contracts_execute)

# ─── shared (no namespace prefix — every persona sees these) ──────────
mcp.tool()(list_adapters)
mcp.tool()(list_strategies)
mcp.tool()(describe_adapter)
mcp.tool()(describe_strategy)
mcp.tool()(get_wallet)
mcp.tool()(get_wallet_balances)
mcp.tool()(wallets)
mcp.tool()(execute)
mcp.tool()(run_script)
mcp.tool()(runner)


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
