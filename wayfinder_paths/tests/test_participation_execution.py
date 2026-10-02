from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from solders.keypair import Keypair

from wayfinder_paths.core.clients.participation_execution import (
    CommandJournal,
    state_lock,
)
from wayfinder_paths.core.clients.PerptoolsClient import PerptoolsClient, account_id
from wayfinder_paths.core.clients.RisexClient import (
    RisexClient,
    integer_units,
    order_hash,
)
from wayfinder_paths.core.clients.WalletClient import WalletClient

# Public test keys, never funded. Tests use only MockTransport.
OWNER = Account.from_key(bytes([1]) * 32)
SESSION = Account.from_key(bytes([2]) * 32)
AUTH = "0x" + "33" * 20
ROUTER = "0x" + "44" * 20


async def sign_session(payload: dict[str, Any]) -> str:
    return SESSION.sign_message(encode_typed_data(full_message=payload)).signature.hex()


def risex(tmp_path: Path, transport: httpx.MockTransport) -> RisexClient:
    return RisexClient(
        account=OWNER.address,
        signer=SESSION.address,
        sign_typed_data=sign_session,
        state_dir=tmp_path,
        chain_id=4153,
        auth_contract=AUTH,
        router=ROUTER,
        transport=transport,
    )


def rise_reads(request: httpx.Request) -> dict[str, Any]:
    if request.url.path.endswith("config"):
        return {
            "chain": {"chain_id": "4153"},
            "addresses": {"auth": AUTH, "router": ROUTER},
        }
    if request.url.path.endswith("eip712-domain"):
        return {
            "name": "RISEx",
            "version": "1",
            "chain_id": "4153",
            "verifying_contract": AUTH,
        }
    return {"nonce_anchor": "2", "bitmap": "1"}


@pytest.mark.asyncio
async def test_rise_signature_nonce_and_no_resubmit(tmp_path: Path) -> None:
    mutations = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": rise_reads(request)})
        mutations.append(json.loads(request.content))
        return httpx.Response(200, json={"data": {"order_id": "order-1"}})

    client = risex(tmp_path, httpx.MockTransport(handle))
    kwargs = {
        "market_id": 1,
        "size_steps": 200,
        "price_ticks": 500000,
        "is_buy": True,
        "client_order_id": 1,
        "operation_id": "first",
    }
    try:
        assert await client.place_order(**kwargs) == await client.place_order(**kwargs)
        assert len(mutations) == 1
        body = mutations[0]
        permit = body["permit"]
        assert permit["nonce_anchor"] == "2" and permit["nonce_bitmap_index"] == 1
        fields = [
            ("account", "address"),
            ("target", "address"),
            ("hash", "bytes32"),
            ("nonceAnchor", "uint48"),
            ("nonceBitmap", "uint8"),
            ("deadline", "uint32"),
        ]
        payload = {
            "domain": await client.domain(),
            "types": {"VerifyWitness": [{"name": k, "type": t} for k, t in fields]},
            "primaryType": "VerifyWitness",
            "message": {
                "account": OWNER.address,
                "target": ROUTER,
                "hash": order_hash(body),
                "nonceAnchor": 2,
                "nonceBitmap": 1,
                "deadline": permit["deadline"],
            },
        }
        assert (
            Account.recover_message(
                encode_typed_data(full_message=payload),
                signature=base64.b64decode(permit["signature"]),
            )
            == SESSION.address
        )
        with pytest.raises(ValueError, match="different parameters"):
            await client.place_order(**{**kwargs, "size_steps": 201})
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_rise_timeout_survives_restart(tmp_path: Path) -> None:
    writes = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": rise_reads(request)})
        writes.append(request)
        raise httpx.ReadTimeout("sensitive response")

    transport = httpx.MockTransport(handle)
    kwargs = {
        "market_id": 1,
        "size_steps": 200,
        "price_ticks": 500000,
        "is_buy": False,
        "client_order_id": 1,
        "operation_id": "first",
        "post_only": False,
    }
    client = risex(tmp_path, transport)
    with pytest.raises(RuntimeError, match="ReadTimeout"):
        await client.place_order(**kwargs)
    await client.close()
    restarted = risex(tmp_path, transport)
    try:
        assert (await restarted.place_order(**kwargs))["status"] == "reconcile_required"
        assert len(writes) == 1
        assert (
            json.loads(writes[0].content)["order_type"] == 1
        )  # price-bounded LIMIT IOC
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_rise_deployment_change_refuses_signing(tmp_path: Path) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        data = rise_reads(request)
        if "addresses" in data:
            data["addresses"]["router"] = OWNER.address
        return httpx.Response(200, json={"data": data})

    client = risex(tmp_path, httpx.MockTransport(handle))
    try:
        with pytest.raises(ValueError, match="deployment changed"):
            await client.place_order(
                market_id=1,
                size_steps=1,
                price_ticks=1,
                is_buy=True,
                client_order_id=1,
                operation_id="x",
            )
        assert not (tmp_path / "commands.json").exists()
    finally:
        await client.close()


@pytest.mark.parametrize(
    "value,step,bits",
    [("0", "1", 32), ("1.2", "1", 32), ("NaN", "1", 32), ("256", "1", 8)],
)
def test_precision_refuses_silent_rounding(value: str, step: str, bits: int) -> None:
    with pytest.raises(ValueError):
        integer_units(value, step, bits)


def test_precision_exact() -> None:
    assert integer_units("0.0002", "0.000001", 32) == 200


def test_journal_lock_and_corruption(tmp_path: Path) -> None:
    with state_lock(tmp_path):
        with pytest.raises(BlockingIOError), state_lock(tmp_path):
            pass
        journal = CommandJournal(tmp_path, "account:A")
        journal.reserve("1", {"size": 1})
        with pytest.raises(ValueError, match="identity"):
            CommandJournal(tmp_path, "account:B")
    (tmp_path / "commands.json").write_text("broken")
    with pytest.raises(ValueError):
        CommandJournal(tmp_path, "account:A")


@pytest.mark.asyncio
@pytest.mark.parametrize("chain", ["EVM", "SOL"])
async def test_orderly_attribution_auth_and_stop_shape(
    tmp_path: Path, chain: str
) -> None:
    key = Keypair.from_seed(bytes([3]) * 32)
    address = (
        OWNER.address
        if chain == "EVM"
        else str(Keypair.from_seed(bytes([4]) * 32).pubkey())
    )
    aid = account_id(address, chain)
    requests = []

    async def sign(message: bytes) -> bytes:
        return bytes(key.sign_message(message))

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/get_account":
            assert request.url.params["broker_id"] == "dextools"
            return httpx.Response(
                200, json={"success": True, "data": {"account_id": aid}}
            )
        requests.append(request)
        assert request.headers["orderly-account-id"] == aid
        if request.url.path == "/api/v1/points/history":
            return httpx.Response(200, json=[{"week": 1}])
        return httpx.Response(
            200, json={"success": True, "data": {"algo_order_id": 17}}
        )

    client = PerptoolsClient(
        account=address,
        chain_type=chain,
        public_key=str(key.pubkey()),
        sign_message=sign,
        state_dir=tmp_path,
        authorization_expires_at=2_000_000_000,
        transport=httpx.MockTransport(handle),
    )
    try:
        kwargs = {
            "symbol": "PERP_BTC_USDC",
            "side": "SELL",
            "trigger_price": 49000,
            "operation_id": "stop",
        }
        await client.place_stop(**kwargs)
        await client.place_stop(**kwargs)
        assert len(requests) == 1
        body = json.loads(requests[0].content)
        assert body["algo_type"] == "POSITIONAL_TP_SL"
        assert body["child_orders"][0]["type"] == "CLOSE_POSITION"
        assert body["child_orders"][0]["reduce_only"] is True
        assert await client.points_history() == [{"week": 1}]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_orderly_rejects_wrong_signer_and_broker(tmp_path: Path) -> None:
    key = Keypair.from_seed(bytes([3]) * 32)
    other = Keypair.from_seed(bytes([4]) * 32)
    client = PerptoolsClient(
        account=OWNER.address,
        chain_type="EVM",
        public_key=str(key.pubkey()),
        sign_message=AsyncMock(
            side_effect=lambda message: bytes(other.sign_message(message))
        ),
        state_dir=tmp_path,
        authorization_expires_at=2_000_000_000,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"data": {"account_id": "wrong"}})
        ),
    )
    try:
        with pytest.raises(ValueError, match="attribution"):
            await client.verify_account()
        with pytest.raises(ValueError, match="signer"):
            await client.positions()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_orderly_evm_key_types_and_scope(tmp_path: Path) -> None:
    seen = []

    async def owner_sign(payload: dict[str, Any]) -> str:
        seen.append(payload)
        return OWNER.sign_message(
            encode_typed_data(full_message=payload)
        ).signature.hex()

    key = Keypair.from_seed(bytes([3]) * 32)
    client = PerptoolsClient(
        account=OWNER.address,
        chain_type="EVM",
        public_key=str(key.pubkey()),
        sign_message=AsyncMock(),
        state_dir=tmp_path,
        authorization_expires_at=100_000,
        testnet=True,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                json={
                    "data": {"ok": True, "account_id": account_id(OWNER.address, "EVM")}
                },
            )
        ),
    )
    try:
        with patch(
            "wayfinder_paths.core.clients.PerptoolsClient.time.time",
            return_value=90_000,
        ):
            await client.authorize_key(
                operation_id="authorize", owner_sign_typed=owner_sign
            )
            await client.authorize_key(
                operation_id="authorize", owner_sign_typed=owner_sign
            )
        assert len(seen) == 1
        assert seen[0]["message"]["scope"] == "read,trading"
        assert seen[0]["domain"]["chainId"] == 421614
        assert [f["name"] for f in seen[0]["types"]["AddOrderlyKey"]] == [
            "brokerId",
            "chainId",
            "orderlyKey",
            "scope",
            "timestamp",
            "expiration",
        ]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_wallet_sign_message_roundtrip() -> None:
    client = WalletClient()
    signature = bytes([1]) * 64
    response = httpx.Response(
        200,
        json={"signature": base64.b64encode(signature).decode(), "encoding": "base64"},
    )
    with patch.object(
        client, "_authed_request", new=AsyncMock(return_value=response)
    ) as request:
        assert await client.sign_svm_message("wallet", b"unsigned message") == signature
        assert request.call_args.kwargs["json"] == {
            "message": base64.b64encode(b"unsigned message").decode()
        }
        with pytest.raises(ValueError):
            await client.sign_svm_message("wallet", b"")


@pytest.mark.asyncio
async def test_rise_explicit_registration_and_stop_cancellation(tmp_path: Path) -> None:
    sent: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": rise_reads(request)})
        sent.append(request)
        return httpx.Response(200, json={"data": {"order_id": "stop-1"}})

    async def owner_sign(payload: dict[str, Any]) -> str:
        assert payload["primaryType"] == "RegisterSigner"
        return OWNER.sign_message(
            encode_typed_data(full_message=payload)
        ).signature.hex()

    client = risex(tmp_path, httpx.MockTransport(handle))
    try:
        with patch(
            "wayfinder_paths.core.clients.RisexClient.time.time", return_value=1000
        ):
            await client.authorize_signer(
                expiration=2000, operation_id="setup", owner_sign=owner_sign
            )
            await client.authorize_signer(
                expiration=2000, operation_id="setup", owner_sign=owner_sign
            )
            await client.place_stop(
                market_id=1,
                is_buy=False,
                size="0.001",
                trigger_price="49000",
                operation_id="stop",
            )
            await client.cancel_stop(order_id="stop-1", operation_id="cancel")
        assert len(sent) == 3
        setup = json.loads(sent[0].content)
        assert setup["nonce_anchor"] == "3"
        assert len(bytes.fromhex(setup["account_signature"][2:])) == 65
        stop = json.loads(sent[1].content)
        assert stop["stop_type"] == 1 and stop["tif"] == 3
        assert len(base64.b64decode(stop["signature"])) == 65
        assert json.loads(sent[2].content)["order_id"] == "stop-1"
        with pytest.raises(ValueError):
            await client.place_stop(
                market_id=1,
                is_buy=False,
                size="NaN",
                trigger_price="1",
                operation_id="invalid",
            )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_orderly_solana_authorization_signs_ascii_digest(tmp_path: Path) -> None:
    from eth_abi import encode
    from eth_utils import keccak

    owner, api = Keypair.from_seed(bytes([5]) * 32), Keypair.from_seed(bytes([6]) * 32)
    seen: list[bytes] = []

    async def owner_sign(message: bytes) -> bytes:
        seen.append(message)
        return bytes(owner.sign_message(message))

    client = PerptoolsClient(
        account=str(owner.pubkey()),
        chain_type="SOL",
        public_key=str(api.pubkey()),
        sign_message=AsyncMock(),
        state_dir=tmp_path,
        authorization_expires_at=100_000,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                json={"data": {"account_id": account_id(str(owner.pubkey()), "SOL")}},
            )
        ),
    )
    try:
        with patch(
            "wayfinder_paths.core.clients.PerptoolsClient.time.time",
            return_value=90_000,
        ):
            await client.authorize_key(
                operation_id="grant", owner_sign_message=owner_sign
            )
        expected = (
            keccak(
                encode(
                    ["bytes32", "bytes32", "bytes32", "uint256", "uint256", "uint256"],
                    [
                        keccak(text="dextools"),
                        keccak(text="ed25519:" + str(api.pubkey())),
                        keccak(text="read,trading"),
                        900900900,
                        90_000_000,
                        100_000_000,
                    ],
                )
            )
            .hex()
            .encode()
        )
        assert seen == [expected] and len(seen[0]) == 64
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_remote_ring_message_signer() -> None:
    from wayfinder_paths.core.utils.wallets import get_wallet_sign_message_callback

    ring = [
        {"address": OWNER.address, "type": "remote", "chain_type": "ethereum"},
        {"address": "Solana-address", "type": "remote", "chain_type": "solana"},
    ]
    with (
        patch(
            "wayfinder_paths.core.utils.wallets.load_wallet_ring",
            new=AsyncMock(return_value=ring),
        ),
        patch(
            "wayfinder_paths.core.utils.wallets.WALLET_CLIENT.sign_svm_message",
            new=AsyncMock(return_value=b"signature"),
        ) as sign,
    ):
        callback, address = await get_wallet_sign_message_callback("primary")
        assert address == "Solana-address"
        assert await callback(b"message") == b"signature"
        sign.assert_awaited_once_with("Solana-address", b"message")
