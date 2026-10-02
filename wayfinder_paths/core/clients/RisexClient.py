"""RISEx REST permits, with explicit setup and crash-safe nonce reservation.

No deposits, OperatorHub allowances, automatic registration, or JWT trading.
Protocol: https://developer.rise.trade/reference/integration
"""

from __future__ import annotations

import base64
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from eth_abi import encode
from eth_utils import keccak

from wayfinder_paths.core.clients.participation_execution import (
    CommandJournal,
    FixedOriginClient,
    TypedSigner,
    signed_typed_data,
    state_lock,
)
from wayfinder_paths.core.clients.ParticipationReadClient import _address


def integer_units(value: str, step: str, bits: int) -> int:
    amount, increment = Decimal(value), Decimal(step)
    if (
        not amount.is_finite()
        or not increment.is_finite()
        or amount <= 0
        or increment <= 0
    ):
        raise ValueError("positive finite size and increment required")
    units = amount / increment
    if units != units.to_integral_value() or not 0 < units < 2**bits:
        raise ValueError("order does not fit market precision or integer range")
    return int(units)


def order_hash(body: dict[str, Any]) -> bytes:
    market, size, price = (
        int(body["market_id"]),
        int(body["size_steps"]),
        int(body["price_ticks"]),
    )
    if not 0 <= market < 2**16 or not 0 < size < 2**32 or not 0 < price < 2**24:
        raise ValueError("invalid packed order dimensions")
    flags = (
        body["side"]
        | (int(body["post_only"]) << 1)
        | (int(body["reduce_only"]) << 2)
        | (2 << 3)
        | (body["order_type"] << 5)
        | (body["time_in_force"] << 6)
    )
    packed = (market << 70) | (size << 38) | (price << 14) | (flags << 6) | 2
    return keccak(
        encode(
            ["bytes32", "uint8", "uint88", "uint16", "uint64", "uint16"],
            [
                keccak(text="RISE_PERPS_PLACE_ORDER_V1"),
                5,
                packed,
                0,
                int(body["client_order_id"]),
                0,
            ],
        )
    )


class RisexClient(FixedOriginClient):
    def __init__(
        self,
        *,
        account: str,
        signer: str,
        sign_typed_data: TypedSigner,
        state_dir: Path,
        chain_id: int,
        auth_contract: str,
        router: str,
        environment: Literal["mainnet", "testnet"] = "mainnet",
        **kwargs: Any,
    ) -> None:
        if environment not in {"mainnet", "testnet"}:
            raise ValueError("unknown RISEx environment")
        super().__init__(
            "https://api.rise.trade"
            if environment == "mainnet"
            else "https://api.testnet.rise.trade",
            **kwargs,
        )
        self.account, self.signer = _address(account), _address(signer)
        self.sign = sign_typed_data
        self.state_dir = state_dir
        self.chain_id, self.auth_contract, self.router = (
            chain_id,
            _address(auth_contract),
            _address(router),
        )
        if environment == "mainnet" and chain_id != 4153:
            raise ValueError("RISEx mainnet chain mismatch")

    async def domain(self) -> dict[str, Any]:
        config = await self._request("GET", "/v1/system/config")
        domain = await self._request("GET", "/v1/auth/eip712-domain")
        addresses = config["addresses"]
        if (
            int(config["chain"]["chain_id"]) != self.chain_id
            or int(domain["chain_id"]) != self.chain_id
            or domain["verifying_contract"].lower() != self.auth_contract.lower()
            or addresses["auth"].lower() != self.auth_contract.lower()
            or addresses["router"].lower() != self.router.lower()
        ):
            raise ValueError("RISEx deployment changed; owner review required")
        return {
            "name": domain["name"],
            "version": domain["version"],
            "chainId": self.chain_id,
            "verifyingContract": self.auth_contract,
        }

    async def _signature(
        self,
        domain: dict[str, Any],
        name: str,
        fields: list[tuple[str, str]],
        message: dict[str, Any],
    ) -> str:
        signature = await signed_typed_data(
            self.sign,
            self.signer,
            {
                "domain": domain,
                "primaryType": name,
                "types": {name: [{"name": k, "type": t} for k, t in fields]},
                "message": message,
            },
        )
        raw = bytes.fromhex(signature.removeprefix("0x"))
        if len(raw) != 65:
            raise ValueError("expected 65-byte ECDSA signature")
        return base64.b64encode(raw).decode("ascii")

    async def _permit(
        self, path: str, body: dict[str, Any], action_hash: bytes, *, operation_id: str
    ) -> dict[str, Any]:
        with state_lock(self.state_dir):
            journal = CommandJournal(
                self.state_dir, f"{self.origin}:{self.account.lower()}"
            )
            # Look up before taking a new nonce or calling a signer.
            prior = journal.data["commands"].get(operation_id)
            if prior:
                return journal.reserve(operation_id, [path, body]) or {}
            domain = await self.domain()
            chain = await self._request("GET", f"/v1/nonce-state/{self.account}")
            anchor = int(chain["nonce_anchor"])
            used = {
                (int(c["anchor"]), int(c["bit"]))
                for c in journal.data["commands"].values()
                if "anchor" in c
            }
            bitmap = (
                int(chain["bitmap"], 0)
                if str(chain["bitmap"]).startswith("0x")
                else int(chain["bitmap"])
            )
            choices = [
                (a, b)
                for a in (anchor, anchor + 1)
                for b in range(208)
                if (a, b) not in used and (a != anchor or not bitmap & (1 << b))
            ]
            if not choices:
                raise ValueError("nonce space exhausted; reconcile in-flight permits")
            anchor, bit = choices[0]
            journal.reserve(operation_id, [path, body], anchor=anchor, bit=bit)
            deadline = int(time.time()) + 60
            message = {
                "account": self.account,
                "target": self.router,
                "hash": "0x" + action_hash.hex(),
                "nonceAnchor": anchor,
                "nonceBitmap": bit,
                "deadline": deadline,
            }
            signature = await self._signature(
                domain,
                "VerifyWitness",
                [
                    ("account", "address"),
                    ("target", "address"),
                    ("hash", "bytes32"),
                    ("nonceAnchor", "uint48"),
                    ("nonceBitmap", "uint8"),
                    ("deadline", "uint32"),
                ],
                message,
            )
            permit = {
                "account": self.account,
                "signer": self.signer,
                "nonce_anchor": str(anchor),
                "nonce_bitmap_index": bit,
                "deadline": deadline,
                "signature": signature,
            }
            response = await self._request(
                "POST", path, json={**body, "permit": permit}
            )
            return journal.complete(operation_id, response)

    async def authorize_signer(
        self, *, expiration: int, operation_id: str, owner_sign: TypedSigner
    ) -> dict[str, Any]:
        """Explicit owner setup on an already-registered collateral account.

        No deposit or OperatorHub allowance is performed. A missing response
        requires session_status reconciliation, not a second authorization.
        """
        if not time.time() < expiration <= time.time() + 7 * 86400:
            raise ValueError("session must expire within seven days")
        with state_lock(self.state_dir):
            journal = CommandJournal(
                self.state_dir, f"{self.origin}:{self.account.lower()}"
            )
            request = ["authorize", self.signer, expiration]
            if operation_id in journal.data["commands"]:
                return journal.reserve(operation_id, request) or {}
            domain = await self.domain()
            chain = await self._request("GET", f"/v1/nonce-state/{self.account}")
            # Setup starts a fresh anchor, per the published registration recipe.
            # Do not invalidate any unresolved permit in the account's journal.
            if any("response" not in c for c in journal.data["commands"].values()):
                raise ValueError("reconcile outstanding commands before registration")
            anchor, bit = int(chain["nonce_anchor"]) + 1, 0
            journal.reserve(operation_id, request, anchor=anchor, bit=bit)
            message = {
                "account": self.account,
                "signer": self.signer,
                "message": "RISEx session key",
                "expiration": expiration,
                "nonceAnchor": anchor,
                "nonceBitmap": bit,
            }
            fields = [
                ("account", "address"),
                ("signer", "address"),
                ("message", "string"),
                ("expiration", "uint32"),
                ("nonceAnchor", "uint48"),
                ("nonceBitmap", "uint8"),
            ]
            owner_signature = await signed_typed_data(
                owner_sign,
                self.account,
                {
                    "domain": domain,
                    "primaryType": "RegisterSigner",
                    "types": {
                        "RegisterSigner": [{"name": k, "type": t} for k, t in fields]
                    },
                    "message": message,
                },
            )
            consent = await self._signature(
                domain,
                "VerifySigner",
                [
                    ("account", "address"),
                    ("nonceAnchor", "uint48"),
                    ("nonceBitmap", "uint8"),
                ],
                {"account": self.account, "nonceAnchor": anchor, "nonceBitmap": bit},
            )
            response = await self._request(
                "POST",
                "/v1/auth/register-signer",
                json={
                    "account": self.account,
                    "signer": self.signer,
                    "message": message["message"],
                    "expiration": str(expiration),
                    "nonce_anchor": str(anchor),
                    "nonce_bitmap_index": bit,
                    "account_signature": "0x" + owner_signature.removeprefix("0x"),
                    "signer_signature": "0x" + base64.b64decode(consent).hex(),
                },
            )
            return journal.complete(operation_id, response)

    async def place_order(
        self,
        *,
        market_id: int,
        size_steps: int,
        price_ticks: int,
        is_buy: bool,
        client_order_id: int,
        operation_id: str,
        post_only: bool = True,
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        if not 0 < client_order_id < 2**64:
            raise ValueError("client order ID must be a nonzero uint64")
        body = {
            "market_id": market_id,
            "size_steps": size_steps,
            "price_ticks": price_ticks,
            "side": 0 if is_buy else 1,
            "post_only": post_only,
            "reduce_only": reduce_only,
            "stp_mode": 2,
            "order_type": 1,
            "time_in_force": 0 if post_only else 3,
            "client_order_id": str(client_order_id),
        }
        return await self._permit(
            "/v1/orders/place", body, order_hash(body), operation_id=operation_id
        )

    async def cancel_order(
        self, *, market_id: int, order_id: str, resting_order_id: int, operation_id: str
    ) -> dict[str, Any]:
        action = keccak(
            encode(
                ["bytes32", "uint256", "uint256"],
                [
                    keccak(text="RISE_PERPS_CANCEL_ORDER_V1"),
                    market_id,
                    resting_order_id,
                ],
            )
        )
        return await self._permit(
            "/v1/orders/cancel",
            {"market_id": market_id, "order_id": order_id},
            action,
            operation_id=operation_id,
        )

    async def place_stop(
        self,
        *,
        market_id: int,
        is_buy: bool,
        size: str,
        trigger_price: str,
        operation_id: str,
    ) -> dict[str, Any]:
        if any(
            not Decimal(v).is_finite() or Decimal(v) <= 0 for v in (size, trigger_price)
        ):
            raise ValueError("stop size and price must be positive")
        message = {
            "account": self.account,
            "marketId": market_id,
            "side": 0 if is_buy else 1,
            "size": size,
            "stopType": 1,
            "stopPrice": trigger_price,
            "limitPrice": "0",
            "orderType": 0,
            "stopPriceOption": 1,
            "tif": 3,
            "sizePercentBps": 0,
        }
        fields = [
            ("account", "address"),
            ("marketId", "uint64"),
            ("side", "uint8"),
            ("size", "string"),
            ("stopType", "uint8"),
            ("stopPrice", "string"),
            ("limitPrice", "string"),
            ("orderType", "uint8"),
            ("stopPriceOption", "uint8"),
            ("tif", "uint8"),
            ("deadline", "uint32"),
            ("sizePercentBps", "uint32"),
        ]
        body = {
            "account": self.account,
            "market_id": str(market_id),
            "side": message["side"],
            "size": size,
            "stop_type": 1,
            "stop_price": trigger_price,
            "limit_price": "0",
            "order_type": 0,
            "stop_price_option": 1,
            "tif": 3,
            "size_percent_bps": 0,
        }
        with state_lock(self.state_dir):
            journal = CommandJournal(
                self.state_dir, f"{self.origin}:{self.account.lower()}"
            )
            prior = journal.reserve(operation_id, ["stop", body])
            if prior is not None:
                return prior
            deadline = int(time.time()) + 60
            signature = await self._signature(
                await self.domain(),
                "PlaceTpslOrder",
                fields,
                {**message, "deadline": deadline},
            )
            result = await self._request(
                "POST",
                "/v1/orders/tpsl",
                json={
                    **body,
                    "deadline": deadline,
                    "signer": self.signer,
                    "signature": signature,
                },
            )
            return journal.complete(operation_id, result)

    async def cancel_stop(self, *, order_id: str, operation_id: str) -> dict[str, Any]:
        with state_lock(self.state_dir):
            journal = CommandJournal(
                self.state_dir, f"{self.origin}:{self.account.lower()}"
            )
            prior = journal.reserve(operation_id, ["cancel_stop", order_id])
            if prior is not None:
                return prior
            deadline = int(time.time()) + 60
            signature = await self._signature(
                await self.domain(),
                "CancelTpslOrder",
                [("account", "address"), ("orderId", "string"), ("deadline", "uint32")],
                {"account": self.account, "orderId": order_id, "deadline": deadline},
            )
            response = await self._request(
                "POST",
                "/v1/orders/tpsl/cancel",
                json={
                    "account": self.account,
                    "signer": self.signer,
                    "order_id": order_id,
                    "deadline": deadline,
                    "signature": signature,
                },
            )
            return journal.complete(operation_id, response)

    async def markets(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/markets")

    async def session_status(self) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/v1/auth/session-key-status",
            params={"account": self.account, "signer": self.signer},
        )

    async def portfolio(self) -> dict[str, Any]:
        data = await self._request(
            "GET", "/v1/portfolio/details", params={"account": self.account}
        )
        if data["account"].lower() != self.account.lower():
            raise ValueError("RISEx account mismatch")
        return data

    async def open_orders(self, market_id: int) -> dict[str, Any]:
        data = await self._request(
            "GET",
            "/v1/orders/open",
            params={"account": self.account, "market_id": market_id},
        )
        if data["account"].lower() != self.account.lower():
            raise ValueError("RISEx order account mismatch")
        return data

    async def orders(self, market_id: int) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/v1/orders",
            params={"account": self.account, "market_id": market_id},
        )

    async def stops(self, market_id: int) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/v1/orders/tpsl",
            params={"account": self.account, "market_id": market_id},
        )

    async def trades(self, market_id: int, page: int = 1) -> dict[str, Any]:
        data = await self._request(
            "GET",
            "/v1/trade-history",
            params={
                "wallet_address": self.account,
                "market_id": market_id,
                "page": page,
                "limit": 1000,
            },
        )
        if data["wallet_address"].lower() != self.account.lower():
            raise ValueError("RISEx fill account mismatch")
        return data
