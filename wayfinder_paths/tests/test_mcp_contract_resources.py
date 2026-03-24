from __future__ import annotations

import json
from pathlib import Path

import pytest

from wayfinder_paths.mcp.resources.contracts import get_contract, get_contract_full
from wayfinder_paths.mcp.state.contract_store import ContractArtifactStore


@pytest.mark.asyncio
async def test_get_contract_returns_abi_summary(tmp_path: Path):
    store = ContractArtifactStore(root=tmp_path)
    store.save(
        chain_id=1,
        contract_address="0x0000000000000000000000000000000000000001",
        contract_name="TestContract",
        deployer_address="0x0000000000000000000000000000000000000002",
        wallet_label="main",
        tx_hash="0xabc",
        source_code="contract TestContract {}",
        abi=[
            {"type": "function", "name": "foo", "inputs": []},
            {"type": "event", "name": "Bar", "inputs": []},
        ],
        bytecode="0x6000",
    )

    original_default = ContractArtifactStore.default
    ContractArtifactStore.default = staticmethod(lambda: store)
    try:
        out = await get_contract("1", "0x0000000000000000000000000000000000000001")
    finally:
        ContractArtifactStore.default = original_default

    result = json.loads(out)
    assert result["metadata"]["contract_name"] == "TestContract"
    assert result["abi_summary"]["function_count"] == 1
    assert result["abi_summary"]["event_count"] == 1
    assert "abi" not in result


@pytest.mark.asyncio
async def test_get_contract_full_returns_full_abi(tmp_path: Path):
    store = ContractArtifactStore(root=tmp_path)
    store.save(
        chain_id=1,
        contract_address="0x0000000000000000000000000000000000000001",
        contract_name="TestContract",
        deployer_address="0x0000000000000000000000000000000000000002",
        wallet_label="main",
        tx_hash="0xabc",
        source_code="contract TestContract {}",
        abi=[{"type": "function", "name": "foo", "inputs": []}],
        bytecode="0x6000",
    )

    original_default = ContractArtifactStore.default
    ContractArtifactStore.default = staticmethod(lambda: store)
    try:
        out = await get_contract_full("1", "0x0000000000000000000000000000000000000001")
    finally:
        ContractArtifactStore.default = original_default

    result = json.loads(out)
    assert result["detail_level"] == "full"
    assert result["abi"][0]["name"] == "foo"
