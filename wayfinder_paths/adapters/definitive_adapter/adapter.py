"""Resting token orders (limit, stops, trailing stop, TP + SL) via Definitive
Flash.

Flash is non-custodial: tokens stay in the wallet, which grants a bounded
ERC-20 allowance and signs each order (EIP-712) and cancel (EIP-191). Nothing
moves until the order fills; a balance drop cancels it. Before signing, the
order Flash returns is checked here: a Flash order on this chain that pulls at
most the requested amount of the spent token from the wallet and pays the
received token back to the wallet.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from eth_abi import decode as abi_decode
from eth_account.messages import (
    _hash_eip191_message,
    encode_defunct,
    encode_typed_data,
)
from eth_utils import keccak, to_checksum_address
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from wayfinder_paths.adapters.definitive_adapter import solana as svm
from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter
from wayfinder_paths.core.clients.DefinitiveClient import (
    DEFINITIVE_CLIENT,
    OPEN_ORDER_STATUSES,
    DefinitiveError,
)
from wayfinder_paths.core.constants.chains import CHAIN_ID_SOLANA
from wayfinder_paths.core.constants.eip712_domain_abi import EIP712_DOMAIN_ABI
from wayfinder_paths.core.constants.erc20_abi import ERC20_ABI
from wayfinder_paths.core.utils.svm import solana_client_from_chain_id
from wayfinder_paths.core.utils.svm_tokens import (
    _resolve_token_program_id,
    get_associated_token_address,
    get_spl_token_balance,
)
from wayfinder_paths.core.utils.svm_transaction import send_svm_versioned_transaction
from wayfinder_paths.core.utils.transaction import send_transaction
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

FLASH_CONTRACT = "0x5d00000873b6BF41539e6f5365B0Ff7d3c368f78"
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
_NATIVE = {"0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee", "0x" + "0" * 40}
_MAX_UINT48 = 2**48 - 1
_DECIMAL = re.compile(r"^\d+(\.\d+)?$")

# EVM chains the Wayfinder relay accepts (Flash ∩ BRAP; mirrors the backend).
DEFINITIVE_CHAINS: dict[int, str] = {
    900: "solana",
    1: "ethereum",
    56: "bsc",
    137: "polygon",
    143: "monad",
    999: "hyperevm",
    4663: "robinhood",
    5042: "arc",
    8453: "base",
    9745: "plasma",
    42161: "arbitrum",
    43114: "avalanche",
}

FLASH_ORDER_FIELDS = [
    {"name": "swapper", "type": "address"},
    {"name": "vault", "type": "address"},
    {"name": "recipient", "type": "address"},
    {"name": "fromToken", "type": "address"},
    {"name": "toToken", "type": "address"},
    {"name": "fromAmount", "type": "uint256"},
    {"name": "salt", "type": "uint256"},
    {"name": "deadline", "type": "uint256"},
]

# order_type -> (side, Flash orderType). Buys spend the from-token for the
# to-token; every other type sells the from-token. Prices are the USD price of
# the token being bought or sold.
ORDER_TYPES: dict[str, tuple[str, str]] = {
    "limit_buy": ("buy", "limit"),
    "limit_sell": ("sell", "limit"),
    "stop_loss": ("sell", "stop-loss"),
    "take_profit": ("sell", "take-profit"),
    "breakout_buy": ("buy", "stop"),
    "trailing_stop": ("sell", "trailing-stop"),
    "bracket": ("sell", "bracket"),
}


def _price(value: Any, name: str) -> str:
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"{name} must be a positive number") from None
    if not d.is_finite() or d <= 0:
        raise ValueError(f"{name} must be a positive number")
    text = format(d.normalize(), "f")
    if not _DECIMAL.match(text):
        raise ValueError(f"{name} must be a positive number")
    return text


def build_flash_request(
    order_type: str,
    *,
    chain_id: int,
    from_token: str,
    to_token: str,
    amount: str,
    limit_price: float | str | None = None,
    trigger_price: float | str | None = None,
    take_profit_price: float | str | None = None,
    stop_loss_price: float | str | None = None,
    trail_percent: float | str | None = None,
    activation_price: float | str | None = None,
) -> dict[str, Any]:
    """The relay's quote/order body (without funderAddress). `amount` is in
    from-token units."""
    if order_type not in ORDER_TYPES:
        raise ValueError(f"order_type must be one of {sorted(ORDER_TYPES)}")
    chain = DEFINITIVE_CHAINS.get(int(chain_id))
    if not chain:
        raise ValueError(f"Token orders aren't supported on chain {chain_id}")
    native_sol = False
    if chain == "solana":
        # SOL trades as the wSOL mint: spent with svmUseNativeSOL, and bought
        # SOL arrives as wSOL (Flash doesn't pay out native SOL).
        native_sol = from_token in (svm.NATIVE_SOL, "native")
        from_token = svm.WSOL_MINT if native_sol else from_token
        to_token = svm.WSOL_MINT if to_token in (svm.NATIVE_SOL, "native") else to_token
        normalize = str
    else:
        if from_token.lower() in _NATIVE:
            raise ValueError(
                "Orders can't spend the native gas token; use its wrapped token"
            )
        normalize = to_checksum_address
    side, flash_type = ORDER_TYPES[order_type]
    target, contra = (to_token, from_token) if side == "buy" else (from_token, to_token)
    request: dict[str, Any] = {
        "targetChain": chain,
        "contraChain": chain,
        "targetAsset": normalize(target),
        "contraAsset": normalize(contra),
        "side": side,
        "qty": _price(amount, "amount"),
        "orderType": flash_type,
    }
    if native_sol:
        request["svmUseNativeSOL"] = True
    if flash_type == "limit":
        request["limitNotionalPrice"] = _price(limit_price, "limit_price")
    elif order_type == "breakout_buy":
        request["triggers"] = [
            {
                "triggerType": "upper",
                "notionalPrice": _price(trigger_price, "trigger_price"),
            }
        ]
    elif order_type == "stop_loss":
        request["triggers"] = [
            {
                "triggerType": "lower",
                "notionalPrice": _price(stop_loss_price, "stop_loss_price"),
            }
        ]
    elif order_type == "take_profit":
        request["triggers"] = [
            {
                "triggerType": "upper",
                "notionalPrice": _price(take_profit_price, "take_profit_price"),
            }
        ]
    elif order_type == "bracket":
        tp = _price(take_profit_price, "take_profit_price")
        sl = _price(stop_loss_price, "stop_loss_price")
        if Decimal(tp) <= Decimal(sl):
            raise ValueError("take_profit_price must be above stop_loss_price")
        request["triggers"] = [
            {"triggerType": "upper", "notionalPrice": tp},
            {"triggerType": "lower", "notionalPrice": sl},
        ]
    else:  # trailing_stop
        pct = Decimal(_price(trail_percent, "trail_percent"))
        if not Decimal("0.5") <= pct <= Decimal("50"):
            raise ValueError("trail_percent must be between 0.5 and 50")
        trailing = {"trailPercent": format((pct / 100).normalize(), "f")}
        if activation_price is not None:
            trailing["activationPrice"] = _price(activation_price, "activation_price")
        request["trailingStop"] = trailing
    return request


def _refuse(reason: str) -> ValueError:
    return ValueError(f"Refusing to sign this order: {reason}")


def parse_flash_order_typed_data(
    raw: str | None, request: dict[str, Any], chain_id: int, max_from_amount: int
) -> dict[str, Any]:
    """Check Flash's EIP-712 order against what we asked for; returns it
    with ints, ready to hash."""
    if not raw:
        raise _refuse("the quote has no order to sign")
    try:
        typed = json.loads(raw)
        domain, message = typed["domain"], typed["message"]
    except (ValueError, KeyError, TypeError):
        raise _refuse("the order is malformed") from None
    if (
        typed.get("primaryType") != "FlashOrder"
        or (typed.get("types") or {}).get("FlashOrder") != FLASH_ORDER_FIELDS
    ):
        raise _refuse("it is not a Flash order")
    try:
        domain_chain = int(domain["chainId"])
        domain_contract = to_checksum_address(domain["verifyingContract"])
        order = {
            k: to_checksum_address(message[k])
            for k in ("swapper", "vault", "recipient", "fromToken", "toToken")
        } | {k: int(message[k]) for k in ("fromAmount", "salt", "deadline")}
    except (ValueError, KeyError, TypeError):
        raise _refuse("it has a malformed field") from None
    if domain.get("name") != "DefinitiveFlashAllowance":
        raise _refuse("unexpected domain")
    if domain_chain != int(chain_id):
        raise _refuse("it is for another chain")
    if domain_contract != FLASH_CONTRACT:
        raise _refuse("it is not for the Flash contract")

    spent, received = (
        (request["contraAsset"], request["targetAsset"])
        if request["side"] == "buy"
        else (request["targetAsset"], request["contraAsset"])
    )
    funder = to_checksum_address(request["funderAddress"])
    if order["swapper"] != funder:
        raise _refuse("it spends another wallet")
    if order["recipient"] != funder:
        raise _refuse("it pays another wallet")
    if order["fromToken"] != to_checksum_address(spent):
        raise _refuse("it spends a different token")
    if order["toToken"] != to_checksum_address(received):
        raise _refuse("it buys a different token")
    if not 0 < order["fromAmount"] <= max_from_amount:
        raise _refuse("it spends more than requested")
    if not int(time.time()) < order["deadline"] <= _MAX_UINT48:
        raise _refuse("its deadline is invalid")
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "FlashOrder": FLASH_ORDER_FIELDS,
        },
        "primaryType": "FlashOrder",
        "domain": {
            "name": "DefinitiveFlashAllowance",
            "version": str(domain.get("version")),
            "chainId": int(chain_id),
            "verifyingContract": FLASH_CONTRACT,
        },
        "message": order,
    }


def flash_order_digest(typed: dict[str, Any]) -> bytes:
    return _hash_eip191_message(encode_typed_data(full_message=typed))


def check_flash_approval(
    approve_tx: dict[str, Any] | None, spent_token: str, max_amount: int
) -> dict[str, Any] | None:
    """Flash's approval must be `approve(Flash|Permit2, amount)` on the spent
    token, for no more than the wallet holds (we always ask for minimal
    allowances)."""
    if not approve_tx:
        return None
    if to_checksum_address(approve_tx["to"]) != to_checksum_address(spent_token):
        raise _refuse("the approval is for another token")
    data = bytes.fromhex(str(approve_tx["data"]).removeprefix("0x"))
    if data[:4] != keccak(text="approve(address,uint256)")[:4]:
        raise _refuse("the approval is malformed")
    try:
        spender, amount = abi_decode(["address", "uint256"], data[4:])
    except Exception:  # noqa: BLE001
        raise _refuse("the approval is malformed") from None
    if to_checksum_address(spender) not in (FLASH_CONTRACT, PERMIT2):
        raise _refuse("the approval is for an unknown spender")
    if amount > max_amount:
        raise _refuse("the approval is larger than the wallet's balance")
    return {"to": to_checksum_address(approve_tx["to"]), "data": approve_tx["data"]}


def kernel_wrapped_digest(
    digest: bytes, *, name: str, version: str, chain_id: int, wallet: str
) -> bytes:
    """How a ZeroDev Kernel (EIP-7702) account validates an EIP-1271 hash."""
    return flash_order_digest(
        {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "Kernel": [{"name": "hash", "type": "bytes32"}],
            },
            "primaryType": "Kernel",
            "domain": {
                "name": name,
                "version": version,
                "chainId": int(chain_id),
                "verifyingContract": to_checksum_address(wallet),
            },
            "message": {"hash": digest},
        }
    )


def flash_cancel_message(order_id: str) -> str:
    return f"Definitive Flash v1 — Cancel Order\nOrder: {order_id}"


class DefinitiveAdapter(BaseAdapter):
    adapter_type: str = "DEFINITIVE"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        sign_callback=None,
        sign_hash_callback=None,
        svm_message_callback=None,
        wallet_address: str | None = None,
    ):
        """EVM wallets pass `sign_callback` (txs) + `sign_hash_callback`;
        a Solana leg passes `sign_callback` (Solana txs) +
        `svm_message_callback` (Ed25519 message -> base58 signature)."""
        super().__init__("definitive_adapter", config)
        self.sign_callback = sign_callback
        self.sign_hash_callback = sign_hash_callback
        self.svm_message_callback = svm_message_callback
        self.wallet_address = (
            wallet_address
            if svm_message_callback is not None or not wallet_address
            else to_checksum_address(wallet_address)
        )

    def _wallet(self) -> str:
        if not self.wallet_address:
            raise ValueError("wallet_address is required")
        return self.wallet_address

    async def _sign_digest(self, chain_id: int, digest: bytes) -> str:
        """Plain ECDSA for a wallet with no code; for a Kernel-delegated
        wallet (EIP-7702), the Kernel-wrapped digest with the 0x00 prefix."""
        if self.sign_hash_callback is None:
            raise ValueError("sign_hash_callback is required")
        wallet = self._wallet()
        async with web3_from_chain_id(int(chain_id)) as w3:
            code = bytes(await w3.eth.get_code(wallet))
            if not code:
                return await self.sign_hash_callback("0x" + digest.hex())
            if not code.startswith(bytes.fromhex("ef0100")):
                raise ValueError("This wallet can't sign token orders on this chain")
            contract = w3.eth.contract(address=wallet, abi=EIP712_DOMAIN_ABI)
            domain = await contract.functions.eip712Domain().call()
        # (fields, name, version, chainId, verifyingContract, salt, extensions)
        name, version, verifying = domain[1], domain[2], domain[4]
        if name != "Kernel" or to_checksum_address(verifying) != wallet:
            raise ValueError("This wallet can't sign token orders on this chain")
        wrapped = kernel_wrapped_digest(
            digest, name=name, version=version, chain_id=chain_id, wallet=wallet
        )
        signature = await self.sign_hash_callback("0x" + wrapped.hex())
        return "0x00" + signature.removeprefix("0x")

    async def place_order(
        self, chain_id: int, request: dict[str, Any], spent_amount_raw: int
    ) -> tuple[bool, dict[str, Any]]:
        """Quote, send Flash's bounded approval if it asks, check, sign,
        submit. Returns (ok, {order_id, approval_tx_hash})."""
        if int(chain_id) == CHAIN_ID_SOLANA:
            return await self._place_solana_order(request, spent_amount_raw)
        wallet = self._wallet()
        body = {**request, "funderAddress": wallet}
        spent = body["contraAsset"] if body["side"] == "buy" else body["targetAsset"]
        approval_hash = None
        try:
            quote = await DEFINITIVE_CLIENT.quote(body)
            evm = quote.get("evm") or {}
            if evm.get("approveTx"):
                async with web3_from_chain_id(int(chain_id)) as w3:
                    token = w3.eth.contract(
                        address=to_checksum_address(spent), abi=ERC20_ABI
                    )
                    balance = await token.functions.balanceOf(wallet).call()
                approval = check_flash_approval(
                    evm["approveTx"], spent, max(balance, spent_amount_raw)
                )
                if approval:
                    if self.sign_callback is None:
                        raise ValueError("sign_callback is required to approve")
                    approval_hash = await send_transaction(
                        {
                            "chainId": int(chain_id),
                            "from": wallet,
                            "to": approval["to"],
                            "data": approval["data"],
                            "value": 0,
                        },
                        self.sign_callback,
                        wait_for_receipt=True,
                    )
                    quote = await DEFINITIVE_CLIENT.quote(body)
                    evm = quote.get("evm") or {}
            if evm.get("permitTypedData"):
                raise _refuse("unexpected Permit2 request")
            typed = parse_flash_order_typed_data(
                evm.get("orderTypedData"), body, chain_id, spent_amount_raw
            )
            signature = await self._sign_digest(chain_id, flash_order_digest(typed))
            order_id = await DEFINITIVE_CLIENT.submit(
                {
                    **body,
                    "quoteId": quote["quoteId"],
                    "userSignature": signature,
                    "evmOrderTypedData": evm["orderTypedData"],
                }
            )
        except (DefinitiveError, ValueError) as exc:
            return False, {"error": str(exc), "approval_tx_hash": approval_hash}
        return True, {"order_id": order_id, "approval_tx_hash": approval_hash}

    async def _place_solana_order(
        self, request: dict[str, Any], spent_amount_raw: int
    ) -> tuple[bool, dict[str, Any]]:
        """Quote; run the checked + rebuilt setup (token accounts, SOL wrap,
        delegate) if needed; re-quote; check the order message against the
        wallet's own token accounts; sign it with the Solana leg; submit."""
        funder = self._wallet()
        body = {**request, "funderAddress": funder}
        spent, received = (
            (body["contraAsset"], body["targetAsset"])
            if body["side"] == "buy"
            else (body["targetAsset"], body["contraAsset"])
        )
        setup_signature = None
        try:
            if self.svm_message_callback is None or self.sign_callback is None:
                raise ValueError("Solana orders need the wallet's Solana leg")
            group = (await DEFINITIVE_CLIENT.config()).get("solana_group") or ""
            if not group:
                raise ValueError("Solana token orders aren't available yet")
            async with solana_client_from_chain_id(CHAIN_ID_SOLANA) as client:
                programs = {
                    mint: await _resolve_token_program_id(
                        client, Pubkey.from_string(mint)
                    )
                    for mint in (spent, received)
                }
            native = body.get("svmUseNativeSOL") is True
            held = await get_spl_token_balance(funder, spent) + (
                spent_amount_raw if native else 0
            )
            quote = await DEFINITIVE_CLIENT.quote(body)
            setup = svm.check_flash_svm_setup(
                quote.get("svm") or {},
                (quote.get("wrap") or {}).get("svmInstructions"),
                funder=funder,
                programs=programs,
                spent_mint=spent,
                max_delegate=max(held, spent_amount_raw),
                max_wrap_lamports=spent_amount_raw if native else 0,
            )
            if setup:
                setup_signature = await self._send_solana_setup(funder, setup)
                quote = await DEFINITIVE_CLIENT.quote(body)
                pending = quote.get("svm") or {}
                if (
                    pending.get("ataSetupIxs")
                    or pending.get("delegateIx")
                    or (quote.get("wrap") or {}).get("svmInstructions")
                ):
                    raise ValueError("Token setup didn't land yet; try again shortly")
            svm_block = quote.get("svm") or {}
            nonce, deadline = svm_block.get("nonce"), svm_block.get("deadline")
            if not nonce or not deadline:
                raise svm._refuse("no Solana order")
            owner = Pubkey.from_string(funder)
            message = svm.check_flash_svm_message(
                svm_block.get("orderMessage"),
                spent_mint=spent,
                max_total=spent_amount_raw,
                expected_hash=svm.flash_svm_order_hash(
                    output_account=get_associated_token_address(
                        owner, Pubkey.from_string(received), programs[received]
                    ),
                    nonce=int(nonce),
                    deadline=int(deadline),
                    input_account=get_associated_token_address(
                        owner, Pubkey.from_string(spent), programs[spent]
                    ),
                    group=group,
                ),
            )
            signature = await self.svm_message_callback(message)
            order_id = await DEFINITIVE_CLIENT.submit(
                {
                    **body,
                    "quoteId": quote["quoteId"],
                    "userSignature": signature,
                    "svmOrderMessage": message,
                    "svmNonce": nonce,
                    "svmDeadline": deadline,
                }
            )
        except (DefinitiveError, ValueError) as exc:
            return False, {"error": str(exc), "setup_tx_signature": setup_signature}
        return True, {"order_id": order_id, "setup_tx_signature": setup_signature}

    async def _send_solana_setup(self, funder: str, instructions: list) -> str:
        async with solana_client_from_chain_id(CHAIN_ID_SOLANA) as client:
            blockhash = (await client.get_latest_blockhash()).value.blockhash
        message = MessageV0.try_compile(
            payer=Pubkey.from_string(funder),
            instructions=instructions,
            address_lookup_table_accounts=[],
            recent_blockhash=blockhash,
        )
        tx = VersionedTransaction.populate(
            message,
            [
                Signature.default()
                for _ in range(message.header.num_required_signatures)
            ],
        )
        details = await send_svm_versioned_transaction(tx, self.sign_callback)
        return str(details["signature"])

    async def list_orders(
        self, *, include_closed: bool = False
    ) -> tuple[bool, list[dict[str, Any]] | str]:
        try:
            orders = await DEFINITIVE_CLIENT.list_orders(
                self._wallet(), () if include_closed else OPEN_ORDER_STATUSES
            )
        except DefinitiveError as exc:
            return False, str(exc)
        return True, orders

    async def cancel_order(self, order_id: str) -> tuple[bool, dict[str, Any]]:
        """Cancel by id (the order's chain is read from Definitive). A cancel
        that races a fill counts as done."""
        try:
            oid = str(uuid.UUID(str(order_id)))
            data = await DEFINITIVE_CLIENT.get_order(self._wallet(), oid)
            chain = ((data.get("order") or {}).get("targetAsset") or {}).get(
                "chain"
            ) or {}
            chain_id = next(
                (
                    cid
                    for cid, name in DEFINITIVE_CHAINS.items()
                    if str(chain.get("id")) == str(cid)
                    or str(chain.get("name", "")).lower() == name
                ),
                None,
            )
            if chain_id is None:
                raise ValueError("Unsupported chain for this order")
            if chain_id == CHAIN_ID_SOLANA:
                if self.svm_message_callback is None:
                    raise ValueError("This order belongs to the wallet's Solana leg")
                signature = await self.svm_message_callback(flash_cancel_message(oid))
            else:
                digest = _hash_eip191_message(
                    encode_defunct(text=flash_cancel_message(oid))
                )
                signature = await self._sign_digest(chain_id, digest)
            try:
                await DEFINITIVE_CLIENT.cancel(self._wallet(), oid, signature)
            except DefinitiveError as exc:
                if "already filled" not in str(exc).lower():
                    raise
                return True, {"order_id": oid, "status": "filled"}
        except (DefinitiveError, ValueError) as exc:
            return False, {"error": str(exc)}
        return True, {"order_id": oid, "status": "cancelled"}
