from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from wayfinder_paths.mcp.utils import read_text_excerpt, read_yaml, repo_root

STRATEGY_ACTIONS = [
    "status",
    "analyze",
    "snapshot",
    "policy",
    "quote",
    "deposit",
    "update",
    "withdraw",
    "exit",
]

ADAPTER_SUMMARIES = {
    "aave_v3_adapter": "Lending and borrowing via Aave V3.",
    "avantis_adapter": "avUSDC ERC-4626 vault on Base for leveraged trading yield.",
    "balance_adapter": "Wallet balance reads and token transfers.",
    "boros_adapter": "Boros lending and position management.",
    "brap_adapter": "Cross-chain swaps and bridges.",
    "ccxt_adapter": "Multi-exchange CEX trading via CCXT.",
    "eigencloud_adapter": "EigenCloud AVS and operator management.",
    "ethena_vault_adapter": "Ethena sUSDe staking vault operations.",
    "etherfi_adapter": "ether.fi liquid restaking and eETH operations.",
    "euler_v2_adapter": "Euler V2 market interactions.",
    "hyperlend_adapter": "HyperLend lending and borrowing.",
    "hyperliquid_adapter": "Hyperliquid market reads and trading actions.",
    "ledger_adapter": "Strategy transaction ledger storage.",
    "lido_adapter": "Lido staking and withdrawal flows.",
    "moonwell_adapter": "Moonwell lending and borrowing.",
    "morpho_adapter": "Morpho Blue market and vault operations.",
    "multicall_adapter": "Batch read calls across contracts.",
    "pendle_adapter": "Pendle PT, YT, and yield market actions.",
    "polymarket_adapter": "Polymarket market reads and trading actions.",
    "pool_adapter": "Pool analytics and read-only inspection.",
    "projectx_adapter": "ProjectX concentrated liquidity management.",
    "token_adapter": "Token metadata and pricing reads.",
    "uniswap_adapter": "Uniswap V3 liquidity management.",
}

STRATEGY_SUMMARIES = {
    "basis_trading_strategy": "Delta-neutral funding rate capture on Hyperliquid.",
    "boros_hype_strategy": "HYPE yield with Boros plus hedge management.",
    "hyperlend_stable_yield_strategy": "Stablecoin allocation across HyperLend markets.",
    "moonwell_wsteth_loop_strategy": "Leveraged wstETH carry trade on Base.",
    "multi_vault_split_strategy": "Multi-vault allocation across lending protocols.",
    "projectx_thbill_usdc_strategy": "USDC allocation into ProjectX T-bill strategy.",
    "stablecoin_yield_strategy": "USDC yield optimization across Base pools.",
}


def _manifest_dir(kind: str) -> Path | None:
    base = repo_root() / "wayfinder_paths" / kind
    return base if base.exists() else None


def _fallback_summary(name: str, kind: str) -> str:
    lookup = STRATEGY_SUMMARIES if kind == "strategies" else ADAPTER_SUMMARIES
    summary = lookup.get(name)
    if summary:
        return summary
    return name.replace("_", " ").strip()


def _capability_preview(values: Any, *, limit: int = 4) -> list[str]:
    if not isinstance(values, list):
        return []
    preview = [str(v).strip() for v in values if str(v).strip()]
    return preview[:limit]


def _adapter_select_view(name: str, manifest: dict[str, Any]) -> dict[str, Any]:
    capabilities = manifest.get("capabilities", [])
    dependencies = manifest.get("dependencies", [])
    return {
        "name": name,
        "kind": "adapter",
        "summary": _fallback_summary(name, "adapters"),
        "when_to_use": "Use when you need protocol-specific reads or actions.",
        "mutating": any(
            ("." in cap and cap.split(".", 1)[1].startswith(("execute", "cancel")))
            or cap in {"transfer", "withdraw"}
            for cap in _capability_preview(capabilities, limit=20)
        ),
        "capabilities": _capability_preview(capabilities),
        "capability_count": len(capabilities) if isinstance(capabilities, list) else 0,
        "dependencies": _capability_preview(dependencies, limit=3),
        "entrypoint": manifest.get("entrypoint"),
        "detail_uri": f"wayfinder://adapters/{name}/full",
    }


def _strategy_select_view(name: str, manifest: dict[str, Any]) -> dict[str, Any]:
    adapters = manifest.get("adapters", [])
    permissions = manifest.get("permissions")
    return {
        "name": name,
        "kind": "strategy",
        "summary": _fallback_summary(name, "strategies"),
        "status": manifest.get("status", "stable"),
        "supported_actions": STRATEGY_ACTIONS,
        "requires_wallet": True,
        "mutating": True,
        "adapter_count": len(adapters) if isinstance(adapters, list) else 0,
        "adapters": [
            str(adapter.get("name")).strip()
            for adapter in (adapters if isinstance(adapters, list) else [])
            if isinstance(adapter, dict) and str(adapter.get("name", "")).strip()
        ][:4],
        "permissions_policy_present": bool(
            isinstance(permissions, dict) and permissions.get("policy")
        ),
        "entrypoint": manifest.get("entrypoint"),
        "detail_uri": f"wayfinder://strategies/{name}/full",
    }


def _full_view(name: str, manifest_path: Path, *, kind: str) -> dict[str, Any]:
    target = manifest_path.parent
    out: dict[str, Any] = {
        "name": name,
        "kind": "strategy" if kind == "strategies" else "adapter",
        "detail_level": "full",
        "manifest": read_yaml(manifest_path),
    }
    readme = read_text_excerpt(target / "README.md")
    if readme:
        out["readme_excerpt"] = readme
    examples_path = target / "examples.json"
    if examples_path.exists():
        try:
            out["examples"] = json.loads(examples_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            out["examples"] = {"error": f"Invalid examples.json for {name}"}
    return out


async def list_adapters() -> str:
    base = _manifest_dir("adapters")
    if base is None:
        return json.dumps({"error": "Adapters directory not found"})
    items: list[dict[str, Any]] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        manifest_path = child / "manifest.yaml"
        if not manifest_path.exists():
            continue
        items.append(_adapter_select_view(child.name, read_yaml(manifest_path)))

    return json.dumps({"adapters": items, "detail_level": "route"}, indent=2)


async def list_strategies() -> str:
    base = _manifest_dir("strategies")
    if base is None:
        return json.dumps({"error": "Strategies directory not found"})
    items: list[dict[str, Any]] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        manifest_path = child / "manifest.yaml"
        if not manifest_path.exists():
            continue
        items.append(_strategy_select_view(child.name, read_yaml(manifest_path)))

    return json.dumps({"strategies": items, "detail_level": "route"}, indent=2)


def _describe(kind: str, name: str, *, full: bool) -> str:
    base = _manifest_dir(kind)
    if base is None:
        return json.dumps({"error": f"{kind.title()} directory not found"})

    singular = "adapter" if kind == "adapters" else "strategy"
    target = base / name
    if not target.exists():
        return json.dumps({"error": f"Unknown {singular}: {name}"})

    manifest_path = target / "manifest.yaml"
    if not manifest_path.exists():
        return json.dumps({"error": f"Missing manifest.yaml for {singular}: {name}"})

    manifest = read_yaml(manifest_path)
    if full:
        return json.dumps(_full_view(name, manifest_path, kind=kind), indent=2)

    if kind == "adapters":
        out = _adapter_select_view(name, manifest)
    else:
        out = _strategy_select_view(name, manifest)
    out["detail_level"] = "select"
    return json.dumps(out, indent=2)


async def describe_adapter(name: str) -> str:
    return _describe("adapters", name, full=False)


async def describe_adapter_full(name: str) -> str:
    return _describe("adapters", name, full=True)


async def describe_strategy(name: str) -> str:
    return _describe("strategies", name, full=False)


async def describe_strategy_full(name: str) -> str:
    return _describe("strategies", name, full=True)
