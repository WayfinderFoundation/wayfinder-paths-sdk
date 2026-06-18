from __future__ import annotations

import base64
import json
import os
import time
from typing import Any

import httpx
from eth_account import Account
from eth_account.signers.local import LocalAccount

from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter

DEFAULT_BASE_URL = "https://swap.paladinfi.com"
BASE_CHAIN_ID = 8453  # Base mainnet — the only chain PaladinFi screens today
DEFAULT_TIMEOUT_SECONDS = 30.0
HTTP_PAYMENT_REQUIRED = 402

# x402 "exact" EVM scheme wire format, verified against a live 402 from
# swap.paladinfi.com. The server returns a base64 `payment-required` header
# whose `accepts[]` carries payTo / amount / asset / extra{name,version}; the
# client replies with a base64 `payment-signature` header carrying a signed
# EIP-3009 TransferWithAuthorization. We assemble and sign it directly with
# eth-account (already a wayfinder-paths dependency) so this adapter needs no
# additional package.
PAYMENT_REQUIRED_HEADER = "payment-required"
PAYMENT_SIGNATURE_HEADER = "payment-signature"
TRANSFER_WITH_AUTHORIZATION_TYPES: dict[str, list[dict[str, str]]] = {
    "TransferWithAuthorization": [
        {"name": "from", "type": "address"},
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"},
        {"name": "nonce", "type": "bytes32"},
    ]
}
# valid_after is backdated to tolerate client/server clock skew (x402 default).
VALIDITY_BACKDATE_SECONDS = 600
# Refuse to sign a payment authorization larger than this (USDC atomic units,
# 6 decimals). The trust-check fee is ~1000 (=$0.001); this caps the blast
# radius if a server/MITM advertises an inflated `amount`. Override via config.
DEFAULT_MAX_FEE_ATOMIC = 100_000  # $0.10


class PaladinTrustAdapter(BaseAdapter):
    """Pre-trade token trust gate — a "trust-check before swap" companion.

    Screens a token contract for honeypot / rug / scam / unverified-source and
    sanction risk before an agent routes a swap into it, mirroring PaladinFi's
    production trust composition (GoPlus token security + Etherscan source
    verification + anomaly heuristics + OFAC SDN) as a single allow/warn/block
    recommendation with the contributing factors.

    Two tiers:
      * ``check_token`` — full composition via the x402-paid /v1/trust-check
        endpoint ($0.001 USDC/call, EIP-3009 on Base). Needs a signer.
      * ``screen_wallet_ofac`` — free OFAC SDN screen via /v1/trust-check/ofac.
        No payment, no signer.

    Config keys (all optional):
      * ``signer`` — an eth-account ``LocalAccount`` used to authorize the
        x402 micropayment, or ``private_key`` (hex) to build one.
      * ``base_url`` — defaults to https://swap.paladinfi.com.
      * ``timeout`` — per-request timeout in seconds (default 30).
    """

    adapter_type: str = "TRUST"

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__("paladin_trust_adapter", config)
        self.base_url = str(self.config.get("base_url", DEFAULT_BASE_URL)).rstrip("/")
        self.timeout = float(self.config.get("timeout", DEFAULT_TIMEOUT_SECONDS))
        self.max_fee_atomic = int(self.config.get("max_fee_atomic", DEFAULT_MAX_FEE_ATOMIC))
        self._signer = self._resolve_signer(self.config)

    @property
    def can_pay(self) -> bool:
        """Whether a signer is configured for the x402-paid check_token call."""
        return self._signer is not None

    @staticmethod
    def _resolve_signer(config: dict[str, Any]) -> LocalAccount | None:
        signer = config.get("signer")
        if signer is not None:
            return signer
        private_key = config.get("private_key")
        if private_key:
            return Account.from_key(private_key)
        return None

    async def check_token(
        self, token_address: str, *, chain_id: int = BASE_CHAIN_ID
    ) -> tuple[bool, dict[str, Any] | str]:
        """Full trust composition for a token contract via x402-paid trust-check.

        On success returns ``(True, response)`` where
        ``response["trust"]["recommendation"]`` is one of ``allow`` / ``warn`` /
        ``block`` — the gate an agent checks before routing a swap into
        ``token_address``.
        """
        if self._signer is None:
            return False, (
                "check_token requires a signer (config['signer'] or "
                "config['private_key']) to pay the x402 trust-check fee; "
                "use screen_wallet_ofac for the free OFAC-only screen"
            )
        return await self._post_x402(
            "/v1/trust-check", {"address": token_address, "chainId": int(chain_id)}
        )

    async def screen_wallet_ofac(
        self, address: str, *, chain_id: int = BASE_CHAIN_ID
    ) -> tuple[bool, dict[str, Any] | str]:
        """Free OFAC SDN screen for a wallet or contract address (no payment).

        ``result["trust"]["recommendation"]`` is ``allow`` or ``block``; treat a
        ``warn`` (returned if the SDN source is transiently unreachable, per the
        API's fail-closed contract) as not-cleared.
        """
        body = {"address": address, "chainId": int(chain_id)}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/v1/trust-check/ofac", json=body
                )
                resp.raise_for_status()
                return True, resp.json()
        except Exception as exc:  # noqa: BLE001
            self.logger.error(f"OFAC screen failed for {address}: {exc}")
            return False, str(exc)

    async def _post_x402(
        self, path: str, body: dict[str, Any]
    ) -> tuple[bool, dict[str, Any] | str]:
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=body)
                if resp.status_code == HTTP_PAYMENT_REQUIRED:
                    payment_header = self._build_payment_header(resp.headers)
                    resp = await client.post(
                        url, json=body, headers={PAYMENT_SIGNATURE_HEADER: payment_header}
                    )
                resp.raise_for_status()
                return True, resp.json()
        except Exception as exc:  # noqa: BLE001
            self.logger.error(f"paid trust-check failed for {body}: {exc}")
            return False, str(exc)

    def _build_payment_header(self, response_headers: Any) -> str:
        """Construct the base64 x402 `payment-signature` header from a 402."""
        required_b64 = response_headers.get(PAYMENT_REQUIRED_HEADER)
        if not required_b64:
            raise ValueError("402 response is missing the payment-required header")
        required = json.loads(base64.b64decode(required_b64))
        option = self._select_payment_option(required["accepts"])
        authorization = self._build_authorization(option)
        signature = self._sign_authorization(authorization, option)
        payload: dict[str, Any] = {
            "x402Version": required.get("x402Version", 2),
            "payload": {"authorization": authorization, "signature": signature},
            "accepted": option,
            "resource": required.get("resource"),
        }
        # Echo server-advertised extensions (e.g. bazaar discovery metadata)
        # unchanged, matching the x402 reference client. They are not part of
        # the signed EIP-3009 authorization.
        extensions = required.get("extensions")
        if extensions is not None:
            payload["extensions"] = extensions
        encoded = json.dumps(payload, separators=(",", ":")).encode()
        return base64.b64encode(encoded).decode()

    @staticmethod
    def _select_payment_option(accepts: list[dict[str, Any]]) -> dict[str, Any]:
        target_network = f"eip155:{BASE_CHAIN_ID}"
        for option in accepts:
            if option.get("scheme") == "exact" and option.get("network") == target_network:
                return option
        raise ValueError(f"no exact {target_network} payment option offered: {accepts}")

    def _build_authorization(self, option: dict[str, Any]) -> dict[str, str]:
        signer = self._require_signer()
        amount = int(option["amount"])
        if amount > self.max_fee_atomic:
            raise ValueError(
                f"refusing to authorize {amount} (> max_fee_atomic "
                f"{self.max_fee_atomic}); the trust-check fee should be ~1000"
            )
        now = int(time.time())
        return {
            "from": signer.address,
            "to": option["payTo"],
            "value": str(amount),
            "validAfter": str(now - VALIDITY_BACKDATE_SECONDS),
            "validBefore": str(now + int(option.get("maxTimeoutSeconds", 300))),
            "nonce": "0x" + os.urandom(32).hex(),
        }

    def _sign_authorization(
        self, authorization: dict[str, str], option: dict[str, Any]
    ) -> str:
        signer = self._require_signer()
        extra = option.get("extra") or {}
        domain = {
            "name": extra.get("name", "USD Coin"),
            "version": extra.get("version", "2"),
            "chainId": int(str(option["network"]).split(":")[1]),
            "verifyingContract": option["asset"],
        }
        message = {
            "from": authorization["from"],
            "to": authorization["to"],
            "value": int(authorization["value"]),
            "validAfter": int(authorization["validAfter"]),
            "validBefore": int(authorization["validBefore"]),
            "nonce": bytes.fromhex(authorization["nonce"].removeprefix("0x")),
        }
        signed = Account.sign_typed_data(
            signer.key, domain, TRANSFER_WITH_AUTHORIZATION_TYPES, message
        )
        return "0x" + signed.signature.hex().removeprefix("0x")

    def _require_signer(self) -> LocalAccount:
        if self._signer is None:
            raise ValueError("signer is not configured")
        return self._signer
