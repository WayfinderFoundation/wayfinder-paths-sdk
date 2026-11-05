from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from web3.exceptions import Web3RPCError

from wayfinder_paths.adapters.avantis_adapter.adapter import AvantisAdapter
from wayfinder_paths.adapters.boros_adapter.client import BorosClient
from wayfinder_paths.core.utils import transaction
from wayfinder_paths.core.utils.tls import verified_ssl_context
from wayfinder_paths.core.utils.web3 import _get_web3


def _rpc(url: str, error: Exception | None = None):
    rpc = MagicMock()
    rpc.provider.endpoint_uri = url
    rpc.eth.send_raw_transaction = AsyncMock(
        side_effect=error, return_value=b"\xab" * 32
    )
    return rpc


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    ["unknown transaction", "unknown transaction type", "not a known transaction"],
)
async def test_rejected_transaction_continues_to_third_rpc(message):
    rpcs = [
        _rpc("https://a.test", ValueError("unavailable")),
        _rpc("https://b.test", ValueError(message)),
        _rpc("https://c.test"),
    ]
    with patch.object(transaction, "web3s_from_chain_id") as ctx:
        ctx.return_value.__aenter__.return_value = rpcs
        assert await transaction.broadcast_transaction(1, b"signed") == "ab" * 32
    rpcs[2].eth.send_raw_transaction.assert_awaited_once()


@pytest.mark.parametrize(
    "message",
    ["already known", "known transaction: 0x123", "transaction already known"],
)
def test_structured_known_transaction_errors(message):
    error = Web3RPCError(
        "RPC error", rpc_response={"error": {"code": -32000, "message": message}}
    )
    assert transaction._already_known(error)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["nonce", "broadcast", "receipt", "estimate"])
async def test_terminal_rpc_failures_never_expose_url_credentials(operation):
    url = "https://rpc.test/v2/SECRET_KEY?token=SECRET_TOKEN"
    error = httpx.HTTPStatusError(
        f"502 Bad Gateway for url '{url}'",
        request=httpx.Request("POST", url),
        response=httpx.Response(502),
    )
    rpc = _rpc(url, error)
    for name in (
        "get_transaction_count",
        "wait_for_transaction_receipt",
        "estimate_gas",
    ):
        setattr(rpc.eth, name, AsyncMock(side_effect=error))
    with (
        patch.object(transaction, "web3s_from_chain_id") as ctx,
        patch.object(transaction, "logger") as log,
    ):
        ctx.return_value.__aenter__.return_value = [rpc]
        with pytest.raises(Exception) as raised:
            if operation == "nonce":
                await transaction.nonce_transaction(
                    {"from": "0x" + "11" * 20, "chainId": 1}
                )
            elif operation == "broadcast":
                await transaction.broadcast_transaction(1, b"signed")
            elif operation == "receipt":
                await transaction.wait_for_transaction_receipt(1, "0xab")
            else:
                await transaction.gas_limit_transaction({"chainId": 1})
        public = str(raised.value) + str(log.mock_calls)
    assert "SECRET_KEY" not in public
    assert "SECRET_TOKEN" not in public
    assert "502" in public


@pytest.mark.parametrize("kind", ["external", "proxy", "fork"])
def test_every_json_rpc_transport_uses_reviewed_ca_bundle(kind, monkeypatch):
    monkeypatch.setattr(
        "wayfinder_paths.core.utils.web3.get_api_base_url", lambda: "https://api.test"
    )
    monkeypatch.setattr(
        "wayfinder_paths.core.utils.web3.get_api_key", lambda: "test-key"
    )
    urls = {
        "external": "https://rpc.test",
        "proxy": "https://api.test/blockchain/rpc/1/",
        "fork": "https://api.test/blockchain/gorlami/fork/1/",
    }
    rpc = _get_web3(urls[kind], 1)
    kwargs = dict(rpc.provider.get_request_kwargs())
    assert kwargs["ssl"] is verified_ssl_context()
    assert (kwargs.get("headers", {}).get("X-API-KEY") == "test-key") is (
        kind != "external"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["boros", "avantis"])
async def test_aiohttp_adapters_use_reviewed_ca_bundle(adapter):
    session = MagicMock()
    response = MagicMock()
    response.status = 200
    response.json = AsyncMock(return_value={"ok": True})
    session.request.return_value.__aenter__.return_value = response
    session.get.return_value.__aenter__.return_value = response
    with patch("aiohttp.ClientSession") as factory:
        factory.return_value.__aenter__.return_value = session
        if adapter == "boros":
            await BorosClient()._http("GET", "/test")
            call = session.request.call_args
        else:
            await AvantisAdapter()._http_get_json("https://api.test/test")
            call = session.get.call_args
    assert call.kwargs["ssl"] is verified_ssl_context()
