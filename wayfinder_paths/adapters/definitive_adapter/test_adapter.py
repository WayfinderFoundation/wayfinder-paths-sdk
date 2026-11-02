import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from eth_abi import encode as abi_encode
from eth_utils import keccak

from wayfinder_paths.adapters.definitive_adapter.adapter import (
    FLASH_CONTRACT,
    DefinitiveAdapter,
    build_flash_request,
    check_flash_approval,
    flash_cancel_message,
    flash_order_digest,
    kernel_wrapped_digest,
    parse_flash_order_typed_data,
)

MODULE = "wayfinder_paths.adapters.definitive_adapter.adapter"
FUNDER = "0x3C2d70AaE1f84bD41991BFa95decF76c4370698B"
OTHER = "0xB7d89BA7627297B2c1D4cFaf765d709dCA455FA8"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WETH = "0x4200000000000000000000000000000000000006"
ORDER_ID = "5b0e6f7a-3c1d-4e2f-9a8b-7c6d5e4f3a2b"
SIG = "0x" + "ab" * 65

REQUEST = {
    **build_flash_request(
        "limit_buy",
        chain_id=8453,
        from_token=USDC,
        to_token=WETH,
        amount="5",
        limit_price=1000,
    ),
    "funderAddress": FUNDER,
}


def typed_data(domain=None, **message):
    """evm.orderTypedData from a live Flash quote (Base, buy WETH, 5 USDC)."""
    return json.dumps(
        {
            "types": {
                "FlashOrder": [
                    {"name": "swapper", "type": "address"},
                    {"name": "vault", "type": "address"},
                    {"name": "recipient", "type": "address"},
                    {"name": "fromToken", "type": "address"},
                    {"name": "toToken", "type": "address"},
                    {"name": "fromAmount", "type": "uint256"},
                    {"name": "salt", "type": "uint256"},
                    {"name": "deadline", "type": "uint256"},
                ]
            },
            "primaryType": "FlashOrder",
            "domain": {
                "name": "DefinitiveFlashAllowance",
                "version": "1",
                "chainId": "8453",
                "verifyingContract": FLASH_CONTRACT,
                **(domain or {}),
            },
            "message": {
                "deadline": "281474976710655",
                "fromAmount": "5000000",
                "fromToken": USDC,
                "recipient": FUNDER,
                "salt": "2168391535104431266176505382684908936764",
                "swapper": FUNDER,
                "toToken": WETH,
                "vault": "0x1074393effFCf1A15e306cD3931F48eDA9ABcd55",
                **message,
            },
        }
    )


def approve(spender, amount, token=USDC):
    data = keccak(text="approve(address,uint256)")[:4] + abi_encode(
        ["address", "uint256"], [spender, amount]
    )
    return {"to": token, "data": "0x" + data.hex()}


def test_request_builder_maps_sides_and_prices():
    assert REQUEST == {
        "targetChain": "base",
        "contraChain": "base",
        "targetAsset": WETH,
        "contraAsset": USDC,
        "side": "buy",
        "qty": "5",
        "orderType": "limit",
        "limitNotionalPrice": "1000",
        "funderAddress": FUNDER,
    }
    trailing = build_flash_request(
        "trailing_stop",
        chain_id=8453,
        from_token=WETH,
        to_token=USDC,
        amount="0.5",
        trail_percent=2.5,
        activation_price=3200,
    )
    assert trailing["side"] == "sell"
    assert trailing["targetAsset"] == WETH
    assert trailing["trailingStop"] == {
        "trailPercent": "0.025",
        "activationPrice": "3200",
    }
    bracket = build_flash_request(
        "bracket",
        chain_id=8453,
        from_token=WETH,
        to_token=USDC,
        amount="1",
        take_profit_price=3500,
        stop_loss_price=2800,
    )
    assert bracket["triggers"] == [
        {"triggerType": "upper", "notionalPrice": "3500"},
        {"triggerType": "lower", "notionalPrice": "2800"},
    ]


@pytest.mark.parametrize(
    ("order_type", "kwargs"),
    [
        ("market", {}),
        ("limit_buy", {"chain_id": 5000}),
        ("limit_buy", {"from_token": "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"}),
        ("limit_buy", {"limit_price": 0}),
        ("limit_buy", {"limit_price": "1e3x"}),
        ("trailing_stop", {"trail_percent": 0.1}),
        ("bracket", {"take_profit_price": 1, "stop_loss_price": 2}),
    ],
)
def test_request_builder_rejects(order_type, kwargs):
    args = {
        "chain_id": 8453,
        "from_token": USDC,
        "to_token": WETH,
        "amount": "5",
        "limit_price": 1000,
        **kwargs,
    }
    with pytest.raises(ValueError):
        build_flash_request(order_type, **args)


def test_live_quote_parses_and_hashes_like_viem():
    typed = parse_flash_order_typed_data(typed_data(), REQUEST, 8453, 5_000_000)
    # Same digest the frontend test pins (viem hashTypedData).
    assert flash_order_digest(typed).hex() == (
        "0c11f9b63c4a7f3437ef280991904215b629de339be3f226dc9fd70dbafa4111"
    )


@pytest.mark.parametrize(
    "raw",
    [
        typed_data({"chainId": "42161"}),
        typed_data({"verifyingContract": OTHER}),
        typed_data({"name": "Permit2"}),
        typed_data(swapper=OTHER),
        typed_data(recipient=OTHER),
        typed_data(fromToken=WETH),
        typed_data(toToken=USDC),
        typed_data(fromAmount="5000001"),
        typed_data(deadline="1"),
        typed_data(deadline=str(2**48)),
        "{",
        None,
    ],
)
def test_bad_orders_are_refused(raw):
    with pytest.raises(ValueError, match="Refusing to sign"):
        parse_flash_order_typed_data(raw, REQUEST, 8453, 5_000_000)


def test_approval_checks():
    assert check_flash_approval(None, USDC, 1) is None
    assert check_flash_approval(approve(FLASH_CONTRACT, 5_000_000), USDC, 5_000_000)
    for tx, token in [
        (approve(OTHER, 1), USDC),
        (approve(FLASH_CONTRACT, 2**256 - 1), USDC),
        (approve(FLASH_CONTRACT, 1, token=WETH), USDC),
    ]:
        with pytest.raises(ValueError, match="Refusing to sign"):
            check_flash_approval(tx, token, 5_000_000)


def test_kernel_wrapping_matches_the_delegate_vector():
    # Checked against isValidSignature on the Kernel 0.3.3 delegate via eth_call.
    digest = bytes.fromhex(
        "aafd596f7030284f14d362ce8c3a202ab13f7fb6424fd6795496fdde8a804f76"
    )
    wrapped = kernel_wrapped_digest(
        digest,
        name="Kernel",
        version="0.3.3",
        chain_id=8453,
        wallet="0xFCAd0B19bB29D4674531d6f115237E16AfCE377c",
    )
    assert wrapped.hex() == (
        "37fd784bbeec19e7e856fab465a4c6ce3b7a956750e4e141fee3f3927c7c2366"
    )


def test_cancel_message_is_byte_exact():
    assert flash_cancel_message(ORDER_ID).encode() == (
        b"Definitive Flash v1 \xe2\x80\x94 Cancel Order\nOrder: " + ORDER_ID.encode()
    )


def fake_web3(code: bytes, domain_name="Kernel", balance=10_000_000, wallet=FUNDER):
    calls = {
        "eip712Domain": ("0x0f", domain_name, "0.3.3", 8453, wallet, b"", []),
        "balanceOf": balance,
    }

    def contract(address, abi):
        name = "balanceOf" if len(abi) > 1 else abi[0]["name"]
        fn = MagicMock(
            return_value=SimpleNamespace(call=AsyncMock(return_value=calls[name]))
        )
        return SimpleNamespace(functions=SimpleNamespace(**{name: fn}))

    w3 = SimpleNamespace(
        eth=SimpleNamespace(get_code=AsyncMock(return_value=code), contract=contract)
    )

    @asynccontextmanager
    async def factory(chain_id):
        yield w3

    return factory


def adapter(sign_hash=None):
    return DefinitiveAdapter(
        sign_callback=AsyncMock(),
        sign_hash_callback=sign_hash or AsyncMock(return_value=SIG),
        wallet_address=FUNDER,
    )


def quote(approve_tx=None):
    return {
        "quoteId": "q1",
        "evm": {
            "approveTx": approve_tx,
            "permitTypedData": None,
            "orderTypedData": typed_data(),
        },
    }


@pytest.mark.asyncio
async def test_place_order_signs_plain_digest_for_an_eoa():
    client = MagicMock(
        quote=AsyncMock(return_value=quote()), submit=AsyncMock(return_value="oid")
    )
    a = adapter()
    with (
        patch(f"{MODULE}.DEFINITIVE_CLIENT", client),
        patch(f"{MODULE}.web3_from_chain_id", fake_web3(b"")),
    ):
        ok, res = await a.place_order(
            8453, {k: v for k, v in REQUEST.items() if k != "funderAddress"}, 5_000_000
        )
    assert ok, res
    assert res == {"order_id": "oid", "approval_tx_hash": None}
    a.sign_hash_callback.assert_awaited_once_with(
        "0x0c11f9b63c4a7f3437ef280991904215b629de339be3f226dc9fd70dbafa4111"
    )
    submitted = client.submit.await_args.args[0]
    assert submitted["userSignature"] == SIG
    assert submitted["funderAddress"] == FUNDER
    assert submitted["quoteId"] == "q1"


@pytest.mark.asyncio
async def test_place_order_approves_then_prefixes_for_a_kernel_wallet():
    client = MagicMock(
        quote=AsyncMock(
            side_effect=[quote(approve(FLASH_CONTRACT, 5_000_000)), quote()]
        ),
        submit=AsyncMock(return_value="oid"),
    )
    a = adapter()
    with (
        patch(f"{MODULE}.DEFINITIVE_CLIENT", client),
        patch(
            f"{MODULE}.web3_from_chain_id",
            fake_web3(bytes.fromhex("ef0100" + "d6" * 20)),
        ),
        patch(
            f"{MODULE}.send_transaction", AsyncMock(return_value="0xapprove")
        ) as send,
    ):
        ok, res = await a.place_order(
            8453, {k: v for k, v in REQUEST.items() if k != "funderAddress"}, 5_000_000
        )
    assert ok, res
    assert res["approval_tx_hash"] == "0xapprove"
    assert send.await_args.args[0]["to"] == USDC
    assert client.submit.await_args.args[0]["userSignature"] == "0x00" + SIG[2:]


@pytest.mark.asyncio
async def test_place_order_never_signs_a_bad_order():
    bad = quote()
    bad["evm"]["orderTypedData"] = typed_data(recipient=OTHER)
    client = MagicMock(quote=AsyncMock(return_value=bad), submit=AsyncMock())
    a = adapter()
    with (
        patch(f"{MODULE}.DEFINITIVE_CLIENT", client),
        patch(f"{MODULE}.web3_from_chain_id", fake_web3(b"")),
    ):
        ok, res = await a.place_order(
            8453, {k: v for k, v in REQUEST.items() if k != "funderAddress"}, 5_000_000
        )
    assert not ok
    assert "pays another wallet" in res["error"]
    a.sign_hash_callback.assert_not_awaited()
    client.submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_contract_wallets_are_refused():
    client = MagicMock(quote=AsyncMock(return_value=quote()), submit=AsyncMock())
    a = adapter()
    with (
        patch(f"{MODULE}.DEFINITIVE_CLIENT", client),
        patch(f"{MODULE}.web3_from_chain_id", fake_web3(b"\x60\x80")),
    ):
        ok, res = await a.place_order(
            8453, {k: v for k, v in REQUEST.items() if k != "funderAddress"}, 5_000_000
        )
    assert not ok and "can't sign" in res["error"]
    a.sign_hash_callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_signs_the_eip191_message_and_treats_a_fill_race_as_done():
    order = {"order": {"targetAsset": {"chain": {"name": "base"}}}}
    from wayfinder_paths.core.clients.DefinitiveClient import DefinitiveError

    client = MagicMock(
        get_order=AsyncMock(return_value=order),
        cancel=AsyncMock(side_effect=DefinitiveError("Order already filled")),
    )
    a = adapter()
    with (
        patch(f"{MODULE}.DEFINITIVE_CLIENT", client),
        patch(f"{MODULE}.web3_from_chain_id", fake_web3(b"")),
    ):
        ok, res = await a.cancel_order(ORDER_ID)
    assert ok and res["status"] == "filled"
    signed = a.sign_hash_callback.await_args.args[0]
    from eth_account.messages import _hash_eip191_message, encode_defunct

    assert (
        signed
        == "0x"
        + _hash_eip191_message(
            encode_defunct(text=flash_cancel_message(ORDER_ID))
        ).hex()
    )


@pytest.mark.asyncio
async def test_cancel_refuses_non_uuid_ids_before_any_call():
    client = MagicMock(get_order=AsyncMock())
    with patch(f"{MODULE}.DEFINITIVE_CLIENT", client):
        ok, res = await adapter().cancel_order("../../orders")
    assert not ok
    client.get_order.assert_not_awaited()
