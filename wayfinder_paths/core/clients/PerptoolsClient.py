"""PERPTools' direct Orderly account, not its separate AI Arena wallet.

Account and API signatures are injected; neither wallet nor Orderly private
keys are accepted. The broker is fixed to the published PERPTools integration.
"""

from __future__ import annotations

import base64
import json
import math
import re
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

from eth_abi import encode
from eth_utils import keccak
from solders.pubkey import Pubkey
from solders.signature import Signature

from wayfinder_paths.core.clients.participation_execution import (
    CommandJournal,
    FixedOriginClient,
    MessageSigner,
    TypedSigner,
    signed_typed_data,
    state_lock,
)
from wayfinder_paths.core.clients.ParticipationReadClient import _address

BROKER_ID = "dextools"


def account_id(account: str, chain_type: str) -> str:
    wallet: bytes | str
    if chain_type == "SOL":
        types, wallet = ["bytes32", "bytes32"], bytes(Pubkey.from_string(account))
    elif chain_type == "EVM":
        types, wallet = ["address", "bytes32"], _address(account)
    else:
        raise ValueError("unsupported account chain")
    return "0x" + keccak(encode(types, [wallet, keccak(text=BROKER_ID)])).hex()


class PerptoolsClient(FixedOriginClient):
    def __init__(
        self,
        *,
        account: str,
        chain_type: Literal["EVM", "SOL"],
        public_key: str,
        sign_message: MessageSigner,
        state_dir: Path,
        authorization_expires_at: float,
        testnet: bool = False,
        **kwargs: Any,
    ) -> None:
        if not math.isfinite(authorization_expires_at) or authorization_expires_at <= 0:
            raise ValueError("authorization expiry must be finite and positive")
        super().__init__(
            "https://testnet-api.orderly.org" if testnet else "https://api.orderly.org",
            **kwargs,
        )
        self.account, self.chain_type = account, chain_type
        self.account_id = account_id(account, chain_type)
        self.public_key = str(Pubkey.from_string(public_key.removeprefix("ed25519:")))
        self.sign_message, self.state_dir = sign_message, state_dir
        self.authorization_expires_at = authorization_expires_at
        self.points_client = FixedOriginClient(
            "https://app.perptools.ai/api", kwargs.get("transport")
        )
        self.testnet = testnet

    async def close(self) -> None:
        await super().close()
        await self.points_client.close()

    async def verify_account(self) -> dict[str, Any]:
        result = await self._request(
            "GET",
            "/v1/get_account",
            params={
                "address": self.account,
                "broker_id": BROKER_ID,
                "chain_type": self.chain_type,
            },
        )
        if result["account_id"].lower() != self.account_id.lower():
            raise ValueError("PERPTools wallet/broker attribution mismatch")
        return result

    async def _signed(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        points: bool = False,
    ) -> Any:
        if time.time() >= self.authorization_expires_at:
            raise ValueError("Orderly authorization expired; owner renewal required")
        query = "?" + urlencode(sorted(params.items())) if params else ""
        payload = (
            json.dumps(body, separators=(",", ":"), allow_nan=False)
            if body is not None
            else ""
        )
        stamp = str(int(time.time() * 1000))
        message = (stamp + method + path + query + payload).encode()
        signature = await self.sign_message(message)
        if len(signature) != 64 or not Signature.from_bytes(signature).verify(
            Pubkey.from_string(self.public_key), message
        ):
            raise ValueError("Orderly signer does not match registered public key")
        headers = {
            "orderly-account-id": self.account_id,
            "orderly-key": "ed25519:" + self.public_key,
            "orderly-timestamp": stamp,
            "orderly-signature": base64.urlsafe_b64encode(signature)
            .decode()
            .rstrip("="),
            "Content-Type": "application/json"
            if body is not None
            else "application/x-www-form-urlencoded",
        }
        client = self.points_client if points else self
        return await client._request(
            method,
            path + query,
            allow_list=points and path == "/v1/points/history",
            headers=headers,
            content=payload.encode() if body is not None else None,
        )

    async def _mutation(
        self,
        method: str,
        path: str,
        *,
        operation_id: str,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with state_lock(self.state_dir):
            journal = CommandJournal(
                self.state_dir, self.origin + ":" + self.account_id
            )
            if operation_id not in journal.data["commands"]:
                await self.verify_account()
            prior = journal.reserve(operation_id, [method, path, body, params])
            if prior is not None:
                return prior
            response = await self._signed(method, path, body=body, params=params)
            return journal.complete(operation_id, response)

    async def place_order(
        self,
        *,
        symbol: str,
        side: Literal["BUY", "SELL"],
        quantity: float,
        price: float,
        client_order_id: str,
        operation_id: str,
        post_only: bool = True,
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        if symbol not in {"PERP_BTC_USDC", "PERP_ETH_USDC"} or side not in {
            "BUY",
            "SELL",
        }:
            raise ValueError("market/side not supported by participation pilot")
        if not all(
            math.isfinite(x) and x > 0 for x in (quantity, price)
        ) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,36}", client_order_id):
            raise ValueError("invalid order dimensions/identifier")
        return await self._mutation(
            "POST",
            "/v1/order",
            operation_id=operation_id,
            body={
                "symbol": symbol,
                "side": side,
                "order_quantity": quantity,
                "order_price": price,
                "order_type": "POST_ONLY" if post_only else "IOC",
                "reduce_only": reduce_only,
                "client_order_id": client_order_id,
            },
        )

    async def cancel_order(
        self, *, symbol: str, client_order_id: str, operation_id: str
    ) -> dict[str, Any]:
        return await self._mutation(
            "DELETE",
            "/v1/client/order",
            operation_id=operation_id,
            params={"symbol": symbol, "client_order_id": client_order_id},
        )

    async def order(self, client_order_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,36}", client_order_id):
            raise ValueError("invalid client order ID")
        return await self._signed("GET", f"/v1/client/order/{client_order_id}")

    async def positions(self) -> dict[str, Any]:
        return await self._signed("GET", "/v1/positions")

    async def orders(self) -> dict[str, Any]:
        return await self._signed("GET", "/v1/orders", params={"status": "INCOMPLETE"})

    async def trades(self, page: int = 1) -> dict[str, Any]:
        return await self._signed(
            "GET", "/v1/trades", params={"page": page, "size": 500}
        )

    async def place_stop(
        self,
        *,
        symbol: str,
        side: Literal["BUY", "SELL"],
        trigger_price: float,
        operation_id: str,
    ) -> dict[str, Any]:
        if (
            symbol not in {"PERP_BTC_USDC", "PERP_ETH_USDC"}
            or side not in {"BUY", "SELL"}
            or not math.isfinite(trigger_price)
            or trigger_price <= 0
        ):
            raise ValueError("invalid protective stop")
        # Native full-position protection, not another entry order. The venue
        # owns the current close size as partial fills grow/shrink the position.
        return await self._mutation(
            "POST",
            "/v1/algo/order",
            operation_id=operation_id,
            body={
                "symbol": symbol,
                "algo_type": "POSITIONAL_TP_SL",
                "trigger_price_type": "MARK_PRICE",
                "child_orders": [
                    {
                        "symbol": symbol,
                        "algo_type": "STOP_LOSS",
                        "side": side,
                        "type": "CLOSE_POSITION",
                        "trigger_price_type": "MARK_PRICE",
                        "trigger_price": trigger_price,
                        "reduce_only": True,
                    }
                ],
            },
        )

    async def cancel_stop(
        self, *, symbol: str, algo_order_id: int, operation_id: str
    ) -> dict[str, Any]:
        return await self._mutation(
            "DELETE",
            "/v1/algo/order",
            operation_id=operation_id,
            params={"symbol": symbol, "algo_order_id": algo_order_id},
        )

    async def stops(self) -> dict[str, Any]:
        return await self._signed(
            "GET", "/v1/algo/orders", params={"status": "INCOMPLETE"}
        )

    async def points(self) -> dict[str, Any]:
        if self.testnet:
            raise ValueError("PERPTools points are not verified on testnet")
        await self.verify_account()
        return await self._signed(
            "GET", "/v1/points", params={"public_key": self.account}, points=True
        )

    async def points_history(self) -> Any:
        if self.testnet:
            raise ValueError("PERPTools points are not verified on testnet")
        await self.verify_account()
        return await self._signed(
            "GET",
            "/v1/points/history",
            params={"public_key": self.account},
            points=True,
        )

    async def authorize_key(
        self,
        *,
        operation_id: str,
        owner_sign_typed: TypedSigner | None = None,
        owner_sign_message: MessageSigner | None = None,
    ) -> dict[str, Any]:
        """Explicit, journaled owner grant; never called by order submission."""
        with state_lock(self.state_dir):
            journal = CommandJournal(
                self.state_dir, self.origin + ":" + self.account_id
            )
            if operation_id not in journal.data["commands"]:
                await self.verify_account()
            prior = journal.reserve(
                operation_id,
                ["authorize", self.public_key, self.authorization_expires_at],
            )
            if prior is not None:
                return prior
            response = await self._authorize_key(
                owner_sign_typed=owner_sign_typed, owner_sign_message=owner_sign_message
            )
            return journal.complete(operation_id, response)

    async def _authorize_key(
        self,
        *,
        owner_sign_typed: TypedSigner | None = None,
        owner_sign_message: MessageSigner | None = None,
    ) -> dict[str, Any]:
        """Explicit setup only. Grants read/trading, NEVER asset/withdrawal scope."""
        now = int(time.time() * 1000)
        expiry = int(self.authorization_expires_at * 1000)
        if not now < expiry <= now + 7 * 86400_000:
            raise ValueError("authorization must expire within seven days")
        chain_id = (
            900900900
            if self.chain_type == "SOL"
            else (421614 if self.testnet else 42161)
        )
        message: dict[str, Any] = {
            "brokerId": BROKER_ID,
            "chainId": chain_id,
            "orderlyKey": "ed25519:" + self.public_key,
            "scope": "read,trading",
            "timestamp": now,
            "expiration": expiry,
        }
        if self.chain_type == "SOL":
            if owner_sign_message is None:
                raise ValueError("Solana owner signer required")
            wire = (
                keccak(
                    encode(
                        [
                            "bytes32",
                            "bytes32",
                            "bytes32",
                            "uint256",
                            "uint256",
                            "uint256",
                        ],
                        [
                            keccak(text=BROKER_ID),
                            keccak(text=message["orderlyKey"]),
                            keccak(text="read,trading"),
                            chain_id,
                            now,
                            expiry,
                        ],
                    )
                )
                .hex()
                .encode()
            )
            raw_signature = await owner_sign_message(wire)
            if not Signature.from_bytes(raw_signature).verify(
                Pubkey.from_string(self.account), wire
            ):
                raise ValueError("Solana owner signer identity mismatch")
            signature = "0x" + raw_signature.hex()
        else:
            if owner_sign_typed is None:
                raise ValueError("EVM owner signer required")
            signature = await signed_typed_data(
                owner_sign_typed,
                self.account,
                {
                    "domain": {
                        "name": "Orderly",
                        "version": "1",
                        "chainId": chain_id,
                        "verifyingContract": "0xCcCCccccCCCCcCCCCCCcCcCccCcCCCcCcccccccC",
                    },
                    "primaryType": "AddOrderlyKey",
                    "types": {
                        "AddOrderlyKey": [
                            {"name": k, "type": t}
                            for k, t in [
                                ("brokerId", "string"),
                                ("chainId", "uint256"),
                                ("orderlyKey", "string"),
                                ("scope", "string"),
                                ("timestamp", "uint64"),
                                ("expiration", "uint64"),
                            ]
                        ]
                    },
                    "message": message,
                },
            )
        return await self._request(
            "POST",
            "/v1/orderly_key",
            json={
                "message": {**message, "chainType": self.chain_type},
                "signature": signature,
                "userAddress": self.account,
            },
        )
