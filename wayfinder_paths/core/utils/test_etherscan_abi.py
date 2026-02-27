from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from wayfinder_paths.core.utils.etherscan import fetch_contract_abi


@pytest.mark.asyncio
async def test_fetch_contract_abi_requires_api_key():
    with patch(
        "wayfinder_paths.core.utils.etherscan.get_etherscan_api_key", return_value=None
    ):
        with pytest.raises(ValueError, match="Etherscan API key required"):
            await fetch_contract_abi(1, "0x" + "11" * 20)


@pytest.mark.asyncio
async def test_fetch_contract_abi_success_parses_result():
    want = [
        {
            "type": "function",
            "name": "foo",
            "stateMutability": "view",
            "inputs": [],
            "outputs": [{"name": "", "type": "uint256"}],
        }
    ]

    async def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/api"
        params = dict(request.url.params)
        assert params["module"] == "contract"
        assert params["action"] == "getabi"
        assert params["address"].startswith("0x")
        assert params["apikey"] == "k"
        assert params["chainid"] == "8453"
        return httpx.Response(
            200,
            json={"status": "1", "message": "OK", "result": json.dumps(want)},
        )

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        out = await fetch_contract_abi(
            8453,
            "0x" + "22" * 20,
            api_key="k",
            client=client,
        )
    assert out == want


@pytest.mark.asyncio
async def test_fetch_contract_abi_unverified_contract_raises():
    async def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "0",
                "message": "NOTOK",
                "result": "Contract source code not verified",
            },
        )

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ValueError, match="not verified"):
            await fetch_contract_abi(
                1,
                "0x" + "22" * 20,
                api_key="k",
                client=client,
            )


@pytest.mark.asyncio
async def test_fetch_contract_abi_invalid_json_raises():
    async def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "1", "message": "OK", "result": "not-json"},
        )

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ValueError, match="parse ABI"):
            await fetch_contract_abi(
                1,
                "0x" + "22" * 20,
                api_key="k",
                client=client,
            )
