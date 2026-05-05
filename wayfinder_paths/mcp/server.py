"""Wayfinder Paths MCP server (FastMCP).

Run locally (via Claude Code .mcp.json):
  poetry run python -m wayfinder_paths.mcp.server

All MCP exports are registered as tools. Resources were nuked because opencode
does not auto-pull resources into model context; the agent only sees them via
the `read_resource` wrapper, which adds a redundant indirection. Plain tools
land in the model's tool spec on every turn.

Every tool name is namespaced as `{namespace}_{name}` so opencode's per-agent
`tools` allowlist can use a single glob (`wayfinder_<namespace>_*: true`)
to scope a persona's surface. Namespaces match the persona groups below:

  - shells       instance ↔ frontend bridge (chart projections, notify, ui ctx)
  - research     alpha-lab, delta-lab
  - hyperliquid  HL perp/spot/HIP-3/HIP-4 reads + writes
  - onchain      token/wallet resolution, swaps
  - polymarket   prediction markets reads + writes
  - contracts    contract compile/deploy/call/abi
  - shared       used by every persona (discovery, run_script, execute, …)
"""

from __future__ import annotations

import asyncio

from mcp.server.fastmcp import FastMCP

from wayfinder_paths.mcp.resources.alpha_lab import get_alpha_types, search_alpha
from wayfinder_paths.mcp.resources.contracts import (
    get_contract,
    list_contracts,
)
from wayfinder_paths.mcp.resources.delta_lab import (
    get_asset_basis_info,
    get_basis_apy_sources,
    get_basis_symbols,
    get_top_apy,
    screen_borrow_routes,
    screen_lending,
    screen_perp,
    screen_price,
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
    get_outcome_user_state,
    get_outcomes,
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
from wayfinder_paths.paths.heartbeat import maybe_heartbeat_installed_paths

mcp = FastMCP("wayfinder")

# ─── shells_* ──────────────────────────────────────────────────────────
# Instance-state bridge between the agent and the shells frontend chart
# panels (projections, frontend context) + user-facing notifications.
mcp.tool(name="shells_get_frontend_context")(get_frontend_context)
mcp.tool(name="shells_add_chart_projection")(add_chart_projection)
mcp.tool(name="shells_remove_chart_projection")(remove_chart_projection)
mcp.tool(name="shells_clear_chart_projections")(clear_chart_projections)
mcp.tool(name="shells_notify")(notify)

# ─── research_* ────────────────────────────────────────────────────────
# Alpha-lab insights, delta-lab snapshots.
# Bulk / time-series delta-lab lives in DELTA_LAB_CLIENT (Python), not MCP —
# see the /using-delta-lab skill.
mcp.tool(name="research_get_alpha_types")(get_alpha_types)
mcp.tool(name="research_search_alpha")(search_alpha)
mcp.tool(name="research_get_basis_symbols")(get_basis_symbols)
mcp.tool(name="research_get_basis_apy_sources")(get_basis_apy_sources)
mcp.tool(name="research_get_top_apy")(get_top_apy)
mcp.tool(name="research_get_asset_basis_info")(get_asset_basis_info)
mcp.tool(name="research_search_delta_lab_assets")(search_delta_lab_assets)
mcp.tool(name="research_screen_price")(screen_price)
mcp.tool(name="research_screen_lending")(screen_lending)
mcp.tool(name="research_screen_perp")(screen_perp)
mcp.tool(name="research_screen_borrow_routes")(screen_borrow_routes)
mcp.tool(name="research_run_strategy")(run_strategy)

# ─── hyperliquid_* ─────────────────────────────────────────────────────
# Default-dex perp, spot, HIP-3 builder perps, HIP-4 outcomes.
# Coin naming reference: /using-hyperliquid-adapter/rules/coin-naming.md.
mcp.tool(name="hyperliquid_wait")(hyperliquid)
mcp.tool(name="hyperliquid_execute")(hyperliquid_execute)
mcp.tool(name="hyperliquid_get_user_state")(get_user_state)
mcp.tool(name="hyperliquid_get_spot_user_state")(get_spot_user_state)
mcp.tool(name="hyperliquid_get_outcome_user_state")(get_outcome_user_state)
mcp.tool(name="hyperliquid_get_mid_prices")(get_mid_prices)
mcp.tool(name="hyperliquid_get_mid_price")(get_mid_price)
mcp.tool(name="hyperliquid_get_markets")(get_markets)
mcp.tool(name="hyperliquid_get_spot_assets")(get_spot_assets)
mcp.tool(name="hyperliquid_get_orderbook")(get_orderbook)
mcp.tool(name="hyperliquid_get_outcomes")(get_outcomes)

# ─── onchain_* ─────────────────────────────────────────────────────────
# Token resolution, wallet/balance lookups, swap quotes.
mcp.tool(name="onchain_resolve_token")(resolve_token)
mcp.tool(name="onchain_get_gas_token")(get_gas_token)
mcp.tool(name="onchain_fuzzy_search_tokens")(fuzzy_search_tokens)
mcp.tool(name="onchain_list_wallets")(list_wallets)
mcp.tool(name="onchain_get_wallet")(get_wallet)
mcp.tool(name="onchain_get_wallet_balances")(get_wallet_balances)
mcp.tool(name="onchain_get_wallet_activity")(get_wallet_activity)
mcp.tool(name="onchain_quote_swap")(quote_swap)

# ─── polymarket_* ──────────────────────────────────────────────────────
# Polymarket CLOB reads + writes (markets, positions, orders).
mcp.tool(name="polymarket_read")(polymarket)
mcp.tool(name="polymarket_execute")(polymarket_execute)

# ─── contracts_* ───────────────────────────────────────────────────────
# Solidity compile/deploy + arbitrary EVM contract reads/writes.
mcp.tool(name="contracts_list")(list_contracts)
mcp.tool(name="contracts_get")(get_contract)
mcp.tool(name="contracts_compile")(compile_contract)
mcp.tool(name="contracts_deploy")(deploy_contract)
mcp.tool(name="contracts_get_abi")(contract_get_abi)
mcp.tool(name="contracts_call")(contract_call)
mcp.tool(name="contracts_execute")(contract_execute)

# ─── shared_* (cross-persona) ──────────────────────────────────────────
# Generic plumbing every persona uses, plus adapter/strategy discovery
# (any persona may need to know what's available).
mcp.tool(name="shared_list_adapters")(list_adapters)
mcp.tool(name="shared_list_strategies")(list_strategies)
mcp.tool(name="shared_describe_adapter")(describe_adapter)
mcp.tool(name="shared_describe_strategy")(describe_strategy)
mcp.tool(name="shared_wallets")(wallets)
mcp.tool(name="shared_execute")(execute)
mcp.tool(name="shared_run_script")(run_script)
mcp.tool(name="shared_runner")(runner)


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
