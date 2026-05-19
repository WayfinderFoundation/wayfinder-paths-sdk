"""Wayfinder Paths MCP server (FastMCP).

Run locally (via Claude Code .mcp.json):
  poetry run python -m wayfinder_paths.mcp.server

All MCP exports are registered as tools. Resources were nuked because opencode
does not auto-pull resources into model context; the agent only sees them via
the `read_resource` wrapper, which adds a redundant indirection. Plain tools
land in the model's tool spec on every turn.

Every tool is named `{namespace}_{name}` so opencode's per-agent `tools`
allowlist can use one glob (`wayfinder_<namespace>_*: true`) per namespace
to scope a persona's surface.

Namespaces:
  - shells       instance ↔ frontend bridge (chart workspace, annotations, notify, ui ctx)
  - research     alpha-lab, delta-lab, backend-mediated web search/fetch
  - hyperliquid  HL perp/spot/HIP-3/HIP-4 reads + writes
  - onchain      token resolution, swaps, wallet activity
  - polymarket   prediction markets reads + writes
  - contracts    contract compile/deploy/call/abi
  - core         cross-persona tools every subagent should allowlist
                 (discovery, wallet reads, run_script, execute, runner)
"""

from __future__ import annotations

import asyncio

from mcp.server.fastmcp import FastMCP

from wayfinder_paths.core.config import is_opencode_instance
from wayfinder_paths.mcp.tools.alpha_lab import (
    research_get_alpha_types,
    research_search_alpha,
)
from wayfinder_paths.mcp.tools.contracts import (
    contracts_compile,
    contracts_deploy,
    contracts_get,
    contracts_list,
)
from wayfinder_paths.mcp.tools.defillama_free import research_defillama_free
from wayfinder_paths.mcp.tools.delta_lab import (
    research_get_asset_basis_info,
    research_get_basis_apy_sources,
    research_get_basis_symbols,
    research_get_top_apy,
    research_search_borrow_routes,
    research_search_delta_lab_assets,
    research_search_lending,
    research_search_perp,
    research_search_price,
)
from wayfinder_paths.mcp.tools.discovery import core_get_adapters_and_strategies
from wayfinder_paths.mcp.tools.evm_contract import (
    contracts_call,
    contracts_execute,
)
from wayfinder_paths.mcp.tools.execute import core_execute
from wayfinder_paths.mcp.tools.goldsky_direct import (
    research_goldsky_graphql,
    research_goldsky_schema,
    research_goldsky_search,
)
from wayfinder_paths.mcp.tools.hyperliquid import (
    hyperliquid_cancel_order,
    hyperliquid_deposit,
    hyperliquid_get_state,
    hyperliquid_place_limit_order,
    hyperliquid_place_market_order,
    hyperliquid_place_trigger_order,
    hyperliquid_search_market,
    hyperliquid_search_mid_prices,
    hyperliquid_update_leverage,
    hyperliquid_withdraw,
)
from wayfinder_paths.mcp.tools.instance_state import (
    shells_add_workspace_chart_annotation,
    shells_add_workspace_chart_overlay,
    shells_add_workspace_chart_series,
    shells_clear_chart_workspace,
    shells_create_chart,
    shells_get_frontend_context,
    shells_search_chart_series,
    shells_set_active_chart,
    shells_set_active_market,
)
from wayfinder_paths.mcp.tools.notify import shells_notify
from wayfinder_paths.mcp.tools.polymarket import (
    polymarket_cancel_order,
    polymarket_deposit,
    polymarket_get_state,
    polymarket_place_limit_order,
    polymarket_place_market_order,
    polymarket_read,
    polymarket_redeem_positions,
    polymarket_withdraw,
)
from wayfinder_paths.mcp.tools.quotes import onchain_quote_swap
from wayfinder_paths.mcp.tools.research_gateway import (
    research_crypto_sentiment,
    research_social_x_search,
    research_web_fetch,
    research_web_search,
)
from wayfinder_paths.mcp.tools.run_script import core_run_script
from wayfinder_paths.mcp.tools.runner import core_runner
from wayfinder_paths.mcp.tools.strategies import core_run_strategy
from wayfinder_paths.mcp.tools.tokens import (
    onchain_fuzzy_search_tokens,
    onchain_get_gas_token,
    onchain_resolve_token,
)
from wayfinder_paths.mcp.tools.wallets import (
    core_get_wallets,
    core_wallets,
    onchain_get_wallet_activity,
)
from wayfinder_paths.paths.heartbeat import maybe_heartbeat_installed_paths

mcp = FastMCP("wayfinder")

# ─── shells_* ──────────────────────────────────────────────────────────
if is_opencode_instance():
    mcp.tool()(shells_get_frontend_context)
    mcp.tool()(shells_search_chart_series)
    mcp.tool()(shells_set_active_market)
    mcp.tool()(shells_create_chart)
    mcp.tool()(shells_set_active_chart)
    mcp.tool()(shells_add_workspace_chart_series)
    mcp.tool()(shells_add_workspace_chart_annotation)
    mcp.tool()(shells_add_workspace_chart_overlay)
    mcp.tool()(shells_clear_chart_workspace)
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
mcp.tool()(research_search_price)
mcp.tool()(research_search_lending)
mcp.tool()(research_search_perp)
mcp.tool()(research_search_borrow_routes)
mcp.tool()(research_web_search)
mcp.tool()(research_web_fetch)
mcp.tool()(research_crypto_sentiment)
mcp.tool()(research_social_x_search)
mcp.tool()(research_defillama_free)
mcp.tool()(research_goldsky_graphql)
mcp.tool()(research_goldsky_search)
mcp.tool()(research_goldsky_schema)

# ─── hyperliquid_* ─────────────────────────────────────────────────────
# Coin naming reference: /using-hyperliquid-adapter/rules/coin-naming.md.
mcp.tool()(hyperliquid_place_market_order)
mcp.tool()(hyperliquid_place_limit_order)
mcp.tool()(hyperliquid_place_trigger_order)
mcp.tool()(hyperliquid_cancel_order)
mcp.tool()(hyperliquid_update_leverage)
mcp.tool()(hyperliquid_deposit)
mcp.tool()(hyperliquid_withdraw)
mcp.tool()(hyperliquid_get_state)
mcp.tool()(hyperliquid_search_market)
mcp.tool()(hyperliquid_search_mid_prices)

# ─── onchain_* ─────────────────────────────────────────────────────────
mcp.tool()(onchain_resolve_token)
mcp.tool()(onchain_get_gas_token)
mcp.tool()(onchain_fuzzy_search_tokens)
mcp.tool()(onchain_get_wallet_activity)
mcp.tool()(onchain_quote_swap)

# ─── polymarket_* ──────────────────────────────────────────────────────
mcp.tool()(polymarket_read)
mcp.tool()(polymarket_get_state)
mcp.tool()(polymarket_deposit)
mcp.tool()(polymarket_withdraw)
mcp.tool()(polymarket_place_market_order)
mcp.tool()(polymarket_place_limit_order)
mcp.tool()(polymarket_cancel_order)
mcp.tool()(polymarket_redeem_positions)

# ─── contracts_* ───────────────────────────────────────────────────────
mcp.tool()(contracts_list)
mcp.tool()(contracts_compile)
mcp.tool()(contracts_deploy)
mcp.tool()(contracts_get)
mcp.tool()(contracts_call)
mcp.tool()(contracts_execute)

# ─── core_* (cross-persona — every subagent should allowlist these) ───
mcp.tool()(core_get_adapters_and_strategies)
mcp.tool()(core_get_wallets)
mcp.tool()(core_wallets)
mcp.tool()(core_execute)
mcp.tool()(core_run_script)
mcp.tool()(core_run_strategy)
mcp.tool()(core_runner)


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
