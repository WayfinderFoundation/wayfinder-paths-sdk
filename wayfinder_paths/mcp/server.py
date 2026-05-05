"""Wayfinder Paths MCP server (FastMCP).

Run locally (via Claude Code .mcp.json):
  poetry run python -m wayfinder_paths.mcp.server

All MCP exports are registered as tools. Resources were nuked because opencode
does not auto-pull resources into model context; the agent only sees them via
the `read_resource` wrapper, which adds a redundant indirection. Plain tools
land in the model's tool spec on every turn.

Registrations are grouped by intended subagent persona so a per-agent
`tools` allowlist in opencode.json can scope each persona's surface:

  - state                 instance ↔ frontend bridge (chart projections, ui ctx)
  - research              discovery, alpha-lab, delta-lab
  - hyperliquid           HL perp/spot/HIP-3/HIP-4 reads + writes
  - onchain-tokens        token/wallet resolution, swaps, polymarket
  - contract-development  contract compile/deploy/call/abi
  - shared                used by every persona (run_script, execute, notify, …)
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

# ─── state ─────────────────────────────────────────────────────────────
# Instance-state bridge between the agent and the frontend chart panels.
mcp.tool()(get_frontend_context)
mcp.tool()(add_chart_projection)
mcp.tool()(remove_chart_projection)
mcp.tool()(clear_chart_projections)

# ─── research ──────────────────────────────────────────────────────────
# Adapter/strategy discovery, alpha-lab insights, delta-lab snapshots.
# Bulk / time-series delta-lab lives in DELTA_LAB_CLIENT (Python), not MCP —
# see the /using-delta-lab skill.
mcp.tool()(list_adapters)
mcp.tool()(list_strategies)
mcp.tool()(describe_adapter)
mcp.tool()(describe_strategy)
mcp.tool()(get_alpha_types)
mcp.tool()(search_alpha)
mcp.tool()(get_basis_symbols)
mcp.tool()(get_basis_apy_sources)
mcp.tool()(get_top_apy)
mcp.tool()(get_asset_basis_info)
mcp.tool()(search_delta_lab_assets)
mcp.tool()(screen_price)
mcp.tool()(screen_lending)
mcp.tool()(screen_perp)
mcp.tool()(screen_borrow_routes)
mcp.tool()(run_strategy)

# ─── hyperliquid ───────────────────────────────────────────────────────
# Default-dex perp, spot, HIP-3 builder perps, HIP-4 outcomes.
# Coin naming reference: /using-hyperliquid-adapter/rules/coin-naming.md.
mcp.tool()(hyperliquid)
mcp.tool()(hyperliquid_execute)
mcp.tool()(get_user_state)
mcp.tool()(get_spot_user_state)
mcp.tool()(get_outcome_user_state)
mcp.tool()(get_mid_prices)
mcp.tool()(get_mid_price)
mcp.tool()(get_markets)
mcp.tool()(get_spot_assets)
mcp.tool()(get_orderbook)
mcp.tool()(get_outcomes)

# ─── onchain-tokens ────────────────────────────────────────────────────
# Token resolution, wallet/balance lookups, swap quotes, polymarket.
mcp.tool()(resolve_token)
mcp.tool()(get_gas_token)
mcp.tool()(fuzzy_search_tokens)
mcp.tool()(list_wallets)
mcp.tool()(get_wallet)
mcp.tool()(get_wallet_balances)
mcp.tool()(get_wallet_activity)
mcp.tool()(quote_swap)
mcp.tool()(polymarket)
mcp.tool()(polymarket_execute)

# ─── contract-development ──────────────────────────────────────────────
# Solidity compile/deploy + arbitrary EVM contract reads/writes.
mcp.tool()(list_contracts)
mcp.tool()(get_contract)
mcp.tool()(compile_contract)
mcp.tool()(deploy_contract)
mcp.tool()(contract_get_abi)
mcp.tool()(contract_call)
mcp.tool()(contract_execute)

# ─── shared (cross-persona) ────────────────────────────────────────────
# Generic plumbing every persona uses.
mcp.tool()(wallets)
mcp.tool()(execute)
mcp.tool()(run_script)
mcp.tool()(runner)
mcp.tool()(notify)


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
