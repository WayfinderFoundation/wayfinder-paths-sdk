from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.core.utils.contracts import deploy_contract


@pytest.mark.asyncio
async def test_deploy_contract_extracts_constructor_args_from_tx_data():
    contract_name = "MyContract"
    abi = [
        {
            "type": "constructor",
            "inputs": [{"name": "x", "type": "uint256"}],
        }
    ]

    std_json = {
        "input": {"language": "Solidity", "sources": {}},
        "output": {
            "contracts": {
                "Contract.sol": {
                    contract_name: {
                        "abi": abi,
                        "evm": {"bytecode": {"object": "6060"}},
                    }
                }
            }
        },
    }

    tx_hash = "0x" + "11" * 32
    contract_address = "0x" + "22" * 20

    seen: dict[str, object] = {}

    async def _fake_verify_on_etherscan(**kwargs):  # noqa: ANN003
        seen.update(kwargs)
        return True

    class _Eth:
        async def get_transaction_receipt(self, got_hash: str) -> dict:
            assert got_hash == tx_hash
            return {"contractAddress": contract_address}

    class _W3:
        eth = _Eth()

    @asynccontextmanager
    async def _fake_web3_from_chain_id(chain_id: int):  # noqa: ANN001
        assert int(chain_id) == 1
        yield _W3()

    with (
        patch(
            "wayfinder_paths.core.utils.contracts.compile_solidity_standard_json",
            return_value=std_json,
        ),
        patch(
            "wayfinder_paths.core.utils.contracts.build_deploy_transaction",
            new=AsyncMock(return_value={"data": "0x6060deadbeef"}),
        ),
        patch(
            "wayfinder_paths.core.utils.contracts.send_transaction",
            new=AsyncMock(return_value=tx_hash),
        ),
        patch(
            "wayfinder_paths.core.utils.contracts.web3_from_chain_id",
            _fake_web3_from_chain_id,
        ),
        patch(
            "wayfinder_paths.core.utils.contracts.verify_on_etherscan",
            new=AsyncMock(side_effect=_fake_verify_on_etherscan),
        ),
    ):
        out = await deploy_contract(
            source_code="// noop",
            contract_name=contract_name,
            constructor_args=[123],
            from_address="0x" + "aa" * 20,
            chain_id=1,
            sign_callback=lambda _tx: b"",  # noqa: ARG005
            verify=True,
            etherscan_api_key="test",
        )

    assert out["tx_hash"] == tx_hash
    assert out["contract_address"] == contract_address
    assert out["verified"] is True
    assert seen["constructor_args_encoded"] == "deadbeef"


@pytest.mark.asyncio
async def test_deploy_contract_verification_failure_is_nonfatal():
    contract_name = "MyContract"
    std_json = {
        "input": {"language": "Solidity", "sources": {}},
        "output": {
            "contracts": {
                "Contract.sol": {
                    contract_name: {
                        "abi": [{"type": "constructor", "inputs": []}],
                        "evm": {"bytecode": {"object": "6060"}},
                    }
                }
            }
        },
    }

    tx_hash = "0x" + "11" * 32
    contract_address = "0x" + "22" * 20

    class _Eth:
        async def get_transaction_receipt(self, got_hash: str) -> dict:
            assert got_hash == tx_hash
            return {"contractAddress": contract_address}

    class _W3:
        eth = _Eth()

    @asynccontextmanager
    async def _fake_web3_from_chain_id(chain_id: int):  # noqa: ANN001
        assert int(chain_id) == 1
        yield _W3()

    with (
        patch(
            "wayfinder_paths.core.utils.contracts.compile_solidity_standard_json",
            return_value=std_json,
        ),
        patch(
            "wayfinder_paths.core.utils.contracts.build_deploy_transaction",
            new=AsyncMock(return_value={"data": "0x6060"}),
        ),
        patch(
            "wayfinder_paths.core.utils.contracts.send_transaction",
            new=AsyncMock(return_value=tx_hash),
        ),
        patch(
            "wayfinder_paths.core.utils.contracts.web3_from_chain_id",
            _fake_web3_from_chain_id,
        ),
        patch(
            "wayfinder_paths.core.utils.contracts.verify_on_etherscan",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
    ):
        out = await deploy_contract(
            source_code="// noop",
            contract_name=contract_name,
            constructor_args=None,
            from_address="0x" + "aa" * 20,
            chain_id=1,
            sign_callback=lambda _tx: b"",  # noqa: ARG005
            verify=True,
            etherscan_api_key="test",
        )

    assert out["tx_hash"] == tx_hash
    assert out["contract_address"] == contract_address
    assert out["verified"] is False
    assert "verification_error" in out and "boom" in str(out["verification_error"])
