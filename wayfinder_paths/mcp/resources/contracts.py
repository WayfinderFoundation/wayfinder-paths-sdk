from __future__ import annotations

import json
from typing import Any

from wayfinder_paths.mcp.state.contract_store import ContractArtifactStore


async def list_contracts() -> str:
    """List all locally-deployed contracts from the artifact store."""
    store = ContractArtifactStore.default()
    entries = store.list_deployments()
    return json.dumps({"contracts": entries, "count": len(entries)}, indent=2)


async def get_contract(chain_id: str, address: str) -> str:
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
    }
    if abi is not None:
        result["abi"] = abi

    abi_path = store.get_abi_path(cid, addr)
    if abi_path is not None:
        result["abi_path"] = str(abi_path)

    return json.dumps(result, indent=2)
