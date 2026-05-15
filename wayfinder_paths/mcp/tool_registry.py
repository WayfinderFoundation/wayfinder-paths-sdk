"""Profile-aware Wayfinder MCP tool registry."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal

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
    hyperliquid_execute,
    hyperliquid_get_state,
    hyperliquid_search_market,
    hyperliquid_search_mid_prices,
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
    polymarket_execute,
    polymarket_get_state,
    polymarket_read,
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

MCPProfile = Literal["all", "main", "research", "visual"]
ToolAccess = Literal["read", "execute", "schedule"]

VALID_PROFILES: frozenset[str] = frozenset({"all", "main", "research", "visual"})


@dataclass(frozen=True)
class ToolEntry:
    profiles: frozenset[str]
    fn: Callable[..., Any]
    access: ToolAccess
    name: str
    opencode_only: bool = False


def _entry(
    profiles: str | Iterable[str],
    fn: Callable[..., Any],
    access: ToolAccess = "read",
    name: str | None = None,
    *,
    opencode_only: bool = False,
) -> ToolEntry:
    if isinstance(profiles, str):
        profile_set = frozenset({profiles})
    else:
        profile_set = frozenset(profiles)
    return ToolEntry(
        profiles=profile_set,
        fn=fn,
        access=access,
        name=name or fn.__name__,
        opencode_only=opencode_only,
    )


TOOL_REGISTRY: tuple[ToolEntry, ...] = (
    # Main / operator / coding.
    _entry("main", core_get_adapters_and_strategies),
    _entry("main", core_get_wallets),
    _entry("main", core_wallets),
    _entry("main", onchain_get_wallet_activity),
    _entry("main", onchain_resolve_token),
    _entry("main", onchain_get_gas_token),
    _entry("main", onchain_fuzzy_search_tokens),
    _entry("main", onchain_quote_swap),
    _entry("main", hyperliquid_get_state),
    _entry("main", hyperliquid_search_market),
    _entry("main", hyperliquid_search_mid_prices),
    _entry("main", polymarket_read),
    _entry("main", polymarket_get_state),
    _entry("main", contracts_list),
    _entry("main", contracts_get),
    _entry("main", contracts_compile),
    _entry("main", contracts_call),
    _entry(("main", "research", "visual"), core_run_script, "execute"),
    _entry("main", core_execute, "execute"),
    _entry("main", hyperliquid_execute, "execute"),
    _entry("main", polymarket_execute, "execute"),
    _entry("main", contracts_deploy, "execute"),
    _entry("main", contracts_execute, "execute"),
    _entry("main", core_run_strategy, "execute"),
    _entry("main", core_runner, "schedule"),
    # Research + Delta Lab + quant data access.
    _entry("research", research_web_search),
    _entry("research", research_web_fetch),
    _entry("research", research_crypto_sentiment),
    _entry("research", research_social_x_search),
    _entry("research", research_defillama_free),
    _entry("research", research_goldsky_graphql),
    _entry("research", research_goldsky_search),
    _entry("research", research_goldsky_schema),
    _entry("research", research_get_alpha_types),
    _entry("research", research_search_alpha),
    _entry("research", research_get_basis_symbols),
    _entry("research", research_get_basis_apy_sources),
    _entry("research", research_get_top_apy),
    _entry("research", research_get_asset_basis_info),
    _entry("research", research_search_delta_lab_assets),
    _entry("research", research_search_price),
    _entry("research", research_search_lending),
    _entry("research", research_search_perp),
    _entry("research", research_search_borrow_routes),
    _entry("research", core_get_adapters_and_strategies),
    # Visual / chart workspace.
    _entry("visual", shells_get_frontend_context, opencode_only=True),
    _entry("visual", shells_search_chart_series, opencode_only=True),
    _entry("visual", shells_set_active_market, opencode_only=True),
    _entry("visual", shells_create_chart, opencode_only=True),
    _entry("visual", shells_set_active_chart, opencode_only=True),
    _entry("visual", shells_add_workspace_chart_series, opencode_only=True),
    _entry("visual", shells_add_workspace_chart_annotation, opencode_only=True),
    _entry("visual", shells_add_workspace_chart_overlay, opencode_only=True),
    _entry("visual", shells_clear_chart_workspace, opencode_only=True),
    _entry("visual", shells_notify, opencode_only=True),
)


def tools_for_profile(
    profile: str,
    *,
    include_opencode_only: bool | None = None,
) -> tuple[ToolEntry, ...]:
    if profile not in VALID_PROFILES:
        raise ValueError(f"Unknown MCP profile: {profile}")

    include_shells = is_opencode_instance() if include_opencode_only is None else include_opencode_only
    tools: list[ToolEntry] = []
    seen: set[str] = set()

    for entry in TOOL_REGISTRY:
        if entry.opencode_only and not include_shells:
            continue
        if profile == "all" or profile in entry.profiles:
            if entry.name in seen:
                continue
            seen.add(entry.name)
            tools.append(entry)

    return tuple(tools)
