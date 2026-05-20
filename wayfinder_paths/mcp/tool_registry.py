"""Wayfinder MCP tool registry."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, cast

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
    research_get_delta_lab_pendle_market,
    research_get_top_apy,
    research_search_borrow_routes,
    research_search_delta_lab_assets,
    research_search_delta_lab_instruments,
    research_search_delta_lab_markets,
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
from wayfinder_paths.mcp.tools.runner import core_runner, core_runner_status
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

ToolAccess = Literal["read", "execute", "schedule"]

_TOOL_ACCESS_ATTR = "_wayfinder_mcp_access"
_OPENCODE_ONLY_ATTR = "_wayfinder_mcp_opencode_only"


def _set_tool_access[F: Callable[..., Any]](fn: F, access: ToolAccess) -> F:
    setattr(fn, _TOOL_ACCESS_ATTR, access)
    return fn


def readonly[F: Callable[..., Any]](fn: F) -> F:
    return _set_tool_access(fn, "read")


def execution_tool[F: Callable[..., Any]](fn: F) -> F:
    return _set_tool_access(fn, "execute")


def schedule_tool[F: Callable[..., Any]](fn: F) -> F:
    return _set_tool_access(fn, "schedule")


def opencode_only[F: Callable[..., Any]](fn: F) -> F:
    setattr(fn, _OPENCODE_ONLY_ATTR, True)
    return fn


def _tool[F: Callable[..., Any]](fn: F, *decorators: Callable[[F], F]) -> F:
    for decorator in decorators:
        fn = decorator(fn)
    return fn


def tool_name(fn: Callable[..., Any]) -> str:
    return fn.__name__


def tool_access(fn: Callable[..., Any]) -> ToolAccess:
    return cast(ToolAccess, getattr(fn, _TOOL_ACCESS_ATTR, "read"))


def is_opencode_only_tool(fn: Callable[..., Any]) -> bool:
    return bool(getattr(fn, _OPENCODE_ONLY_ATTR, False))


TOOL_REGISTRY: tuple[Callable[..., Any], ...] = (
    # contracts_*
    _tool(contracts_list),
    _tool(contracts_get),
    _tool(contracts_compile),
    _tool(contracts_call),
    _tool(contracts_deploy, execution_tool),
    _tool(contracts_execute, execution_tool),
    # core_*
    _tool(core_get_adapters_and_strategies),
    _tool(core_get_wallets),
    _tool(core_wallets),
    _tool(core_execute, execution_tool),
    _tool(core_run_script, execution_tool),
    _tool(core_run_strategy, execution_tool),
    _tool(core_runner_status),
    _tool(core_runner, schedule_tool),
    # hyperliquid_*
    _tool(hyperliquid_get_state),
    _tool(hyperliquid_search_market),
    _tool(hyperliquid_search_mid_prices),
    _tool(hyperliquid_place_market_order, execution_tool),
    _tool(hyperliquid_place_limit_order, execution_tool),
    _tool(hyperliquid_place_trigger_order, execution_tool),
    _tool(hyperliquid_cancel_order, execution_tool),
    _tool(hyperliquid_update_leverage, execution_tool),
    _tool(hyperliquid_deposit, execution_tool),
    _tool(hyperliquid_withdraw, execution_tool),
    # onchain_*
    _tool(onchain_get_wallet_activity),
    _tool(onchain_resolve_token),
    _tool(onchain_get_gas_token),
    _tool(onchain_fuzzy_search_tokens),
    _tool(onchain_quote_swap),
    # polymarket_*
    _tool(polymarket_read),
    _tool(polymarket_get_state),
    _tool(polymarket_place_market_order, execution_tool),
    _tool(polymarket_place_limit_order, execution_tool),
    _tool(polymarket_cancel_order, execution_tool),
    _tool(polymarket_deposit, execution_tool),
    _tool(polymarket_withdraw, execution_tool),
    _tool(polymarket_redeem_positions, execution_tool),
    # research_*
    _tool(research_web_search),
    _tool(research_web_fetch),
    _tool(research_crypto_sentiment),
    _tool(research_social_x_search),
    _tool(research_defillama_free),
    _tool(research_goldsky_graphql),
    _tool(research_goldsky_search),
    _tool(research_goldsky_schema),
    _tool(research_get_alpha_types),
    _tool(research_search_alpha),
    _tool(research_get_basis_symbols),
    _tool(research_get_basis_apy_sources),
    _tool(research_get_top_apy),
    _tool(research_get_asset_basis_info),
    _tool(research_search_delta_lab_assets),
    _tool(research_search_delta_lab_markets),
    _tool(research_search_delta_lab_instruments),
    _tool(research_get_delta_lab_pendle_market),
    _tool(research_search_price),
    _tool(research_search_lending),
    _tool(research_search_perp),
    _tool(research_search_borrow_routes),
    # shells_* (opencode-only)
    _tool(shells_get_frontend_context, opencode_only),
    _tool(shells_search_chart_series, opencode_only),
    _tool(shells_set_active_market, opencode_only),
    _tool(shells_create_chart, opencode_only),
    _tool(shells_set_active_chart, opencode_only),
    _tool(shells_add_workspace_chart_series, opencode_only),
    _tool(shells_add_workspace_chart_annotation, opencode_only),
    _tool(shells_add_workspace_chart_overlay, opencode_only),
    _tool(shells_clear_chart_workspace, opencode_only),
    _tool(shells_notify, opencode_only),
)


def tools_for_mcp() -> tuple[Callable[..., Any], ...]:
    include_shells = is_opencode_instance()
    tools: list[Callable[..., Any]] = []
    seen: set[str] = set()

    for fn in TOOL_REGISTRY:
        if is_opencode_only_tool(fn) and not include_shells:
            continue
        name = tool_name(fn)
        if name in seen:
            continue
        seen.add(name)
        tools.append(fn)

    return tuple(tools)
