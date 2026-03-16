from __future__ import annotations

import json
from typing import Any

from wayfinder_paths.mcp.state.contract_store import ContractArtifactStore
from wayfinder_paths.mcp.utils import abi_function_signature


def _abi_preview(abi: list[dict[str, Any]], *, limit: int = 8) -> dict[str, Any]:
    functions: list[str] = []
    event_count = 0
    for entry in abi:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("type") or "").strip()
        if kind == "function":
            functions.append(abi_function_signature(entry))
        elif kind == "event":
            event_count += 1
    return {
        "function_count": len(functions),
        "event_count": event_count,
        "functions": functions[:limit],
    }


async def list_contracts() -> str:
    """List all locally-deployed contracts from the artifact store."""
    store = ContractArtifactStore.default()
    entries = store.list_deployments()
    return json.dumps(
        {"contracts": entries, "count": len(entries), "detail_level": "route"},
        indent=2,
    )


async def get_contract(chain_id: str, address: str) -> str:
    """Get compact metadata and ABI summary for a deployed contract."""
    store = ContractArtifactStore.default()
    cid = int(chain_id)
    addr = str(address).strip().lower()

    metadata = store.get_metadata(cid, addr)
    if not metadata:
        return json.dumps(
            {"error": f"No artifacts found for {address} on chain {chain_id}"}
        )

    abi = store.get_abi(cid, addr)
    result: dict[str, Any] = {"metadata": metadata, "detail_level": "select"}
    if abi is not None:
        result["abi_summary"] = _abi_preview(abi)
        result["detail_uri"] = f"wayfinder://contracts/{chain_id}/{address}/full"

    abi_path = store.get_abi_path(cid, addr)
    if abi_path is not None:
        result["abi_path"] = str(abi_path)

    return json.dumps(result, indent=2)


async def get_contract_full(chain_id: str, address: str) -> str:
    """Get full metadata and ABI for a deployed contract."""
    store = ContractArtifactStore.default()
    cid = int(chain_id)
    addr = str(address).strip().lower()

    metadata = store.get_metadata(cid, addr)
    if not metadata:
        return json.dumps(
            {"error": f"No artifacts found for {address} on chain {chain_id}"}
        )

    abi = store.get_abi(cid, addr)
    result: dict[str, Any] = {
        "metadata": metadata,
        "detail_level": "full",
    }
    if abi is not None:
        result["abi"] = abi

    abi_path = store.get_abi_path(cid, addr)
    if abi_path is not None:
        result["abi_path"] = str(abi_path)

    return json.dumps(result, indent=2)
