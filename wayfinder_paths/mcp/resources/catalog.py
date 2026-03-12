from __future__ import annotations

import json
from typing import Any

INTENT_CATALOG: dict[str, dict[str, Any]] = {
    "wallet_inspection": {
        "summary": "Inspect wallets, tracked protocols, balances, and recent activity.",
        "next_steps": [
            {
                "name": "list_wallets",
                "kind": "resource",
                "summary": "List configured wallets and tracked protocols.",
                "detail_uri": "wayfinder://wallets",
                "required_inputs": [],
                "mutating": False,
            },
            {
                "name": "get_wallet_balances",
                "kind": "resource",
                "summary": "Read compact balance totals, chain breakdown, and top positions.",
                "detail_uri": "wayfinder://balances/{label}",
                "required_inputs": ["label"],
                "mutating": False,
            },
            {
                "name": "get_wallet_activity",
                "kind": "resource",
                "summary": "Read recent wallet activity with a compact event shape.",
                "detail_uri": "wayfinder://activity/{label}",
                "required_inputs": ["label"],
                "mutating": False,
            },
        ],
    },
    "token_lookup": {
        "summary": "Resolve token identities and search for likely matches.",
        "next_steps": [
            {
                "name": "resolve_token",
                "kind": "resource",
                "summary": "Resolve a single token by id, address, or query.",
                "detail_uri": "wayfinder://tokens/resolve/{query}",
                "required_inputs": ["query"],
                "mutating": False,
            },
            {
                "name": "fuzzy_search_tokens",
                "kind": "resource",
                "summary": "Search top token matches with compact identity fields.",
                "detail_uri": "wayfinder://tokens/search/{chain_code}/{query}",
                "required_inputs": ["chain_code", "query"],
                "mutating": False,
            },
            {
                "name": "get_gas_token",
                "kind": "resource",
                "summary": "Resolve the gas token for a chain.",
                "detail_uri": "wayfinder://tokens/gas/{chain_code}",
                "required_inputs": ["chain_code"],
                "mutating": False,
            },
        ],
    },
    "swap_send_bridge": {
        "summary": "Quote or execute transfers, swaps, and Hyperliquid deposits.",
        "next_steps": [
            {
                "name": "execute",
                "kind": "tool",
                "summary": "General transfer, swap, and Hyperliquid deposit execution.",
                "detail_uri": "tool://execute",
                "required_inputs": ["kind", "wallet_label", "amount"],
                "mutating": True,
            },
            {
                "name": "quote_swap",
                "kind": "tool",
                "summary": "Read-only swap quote lookup.",
                "detail_uri": "tool://quote_swap",
                "required_inputs": ["wallet_label", "from_token", "to_token", "amount"],
                "mutating": False,
            },
        ],
    },
    "hyperliquid_trading": {
        "summary": "Inspect Hyperliquid markets or execute account actions.",
        "next_steps": [
            {
                "name": "get_markets",
                "kind": "resource",
                "summary": "Read market metadata.",
                "detail_uri": "wayfinder://hyperliquid/markets",
                "required_inputs": [],
                "mutating": False,
            },
            {
                "name": "hyperliquid",
                "kind": "tool",
                "summary": "Read Hyperliquid market and account information.",
                "detail_uri": "tool://hyperliquid",
                "required_inputs": ["action"],
                "mutating": False,
            },
            {
                "name": "hyperliquid_execute",
                "kind": "tool",
                "summary": "Execute Hyperliquid orders, transfers, or withdrawals.",
                "detail_uri": "tool://hyperliquid_execute",
                "required_inputs": ["action"],
                "mutating": True,
            },
        ],
    },
    "strategy_actions": {
        "summary": "Inspect or run packaged strategies.",
        "next_steps": [
            {
                "name": "list_strategies",
                "kind": "resource",
                "summary": "Read compact strategy summaries for routing.",
                "detail_uri": "wayfinder://strategies",
                "required_inputs": [],
                "mutating": False,
            },
            {
                "name": "describe_strategy",
                "kind": "resource",
                "summary": "Read strategy action support and execution requirements.",
                "detail_uri": "wayfinder://strategies/{name}",
                "required_inputs": ["name"],
                "mutating": False,
            },
            {
                "name": "run_strategy",
                "kind": "tool",
                "summary": "Execute a specific strategy action.",
                "detail_uri": "tool://run_strategy",
                "required_inputs": ["strategy", "action"],
                "mutating": True,
            },
        ],
    },
    "contract_workflows": {
        "summary": "Inspect, compile, deploy, call, or execute contracts.",
        "next_steps": [
            {
                "name": "list_contracts",
                "kind": "resource",
                "summary": "List locally tracked contract deployments.",
                "detail_uri": "wayfinder://contracts",
                "required_inputs": [],
                "mutating": False,
            },
            {
                "name": "get_contract",
                "kind": "resource",
                "summary": "Read compact contract metadata and ABI summary.",
                "detail_uri": "wayfinder://contracts/{chain_id}/{address}",
                "required_inputs": ["chain_id", "address"],
                "mutating": False,
            },
            {
                "name": "contract_execute",
                "kind": "tool",
                "summary": "Execute a contract function on-chain.",
                "detail_uri": "tool://contract_execute",
                "required_inputs": ["chain_id", "contract_address", "function"],
                "mutating": True,
            },
        ],
    },
    "runner_operations": {
        "summary": "Inspect or control the local runner daemon.",
        "next_steps": [
            {
                "name": "runner",
                "kind": "tool",
                "summary": "Control jobs and daemon lifecycle.",
                "detail_uri": "tool://runner",
                "required_inputs": ["action"],
                "mutating": True,
            }
        ],
    },
}


async def list_intents() -> str:
    intents = [
        {
            "intent": name,
            "summary": payload["summary"],
            "detail_uri": f"wayfinder://guide/{name}",
        }
        for name, payload in sorted(INTENT_CATALOG.items())
    ]
    return json.dumps({"intents": intents}, indent=2)


async def guide_intent(intent: str) -> str:
    key = str(intent).strip().lower().replace("-", "_").replace(" ", "_")
    payload = INTENT_CATALOG.get(key)
    if payload is None:
        return json.dumps(
            {
                "error": f"Unknown intent: {intent}",
                "available_intents": sorted(INTENT_CATALOG.keys()),
            },
            indent=2,
        )

    return json.dumps(
        {
            "intent": key,
            "summary": payload["summary"],
            "next_steps": payload["next_steps"],
        },
        indent=2,
    )
