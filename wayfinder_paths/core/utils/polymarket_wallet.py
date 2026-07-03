"""Canonical Polymarket deposit-wallet resolution.

Polymarket's deposit-wallet factory (0x00000000000Fb5C9ADea0298D729A0CB3823Cc07,
Polygon) upgraded its implementation on 2026-06-29 from an ERC-1967 CREATE2
scheme to a beacon-proxy scheme, changing the derived address for every wallet
not yet deployed. Resolution semantics:

  - a contract already deployed at the legacy derivation (pre-upgrade wallet)
    stays canonical;
  - otherwise the factory's own predictWalletAddress(bytes32 id) is canonical.

Local derivation is NEVER a fallback — sending funds to an undeployed
legacy-scheme address strands them (only Polymarket's factory admins could
resurrect that derivation).
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from eth_abi import encode as abi_encode
from eth_utils.address import to_checksum_address

from wayfinder_paths.core.constants.polymarket import (
    POLYGON_P_USDC_PROXY_ADDRESS,
    POLYGON_USDC_E_ADDRESS,
    POLYMARKET_DEPOSIT_WALLET_FACTORY,
    derive_legacy_deposit_wallet,
    polymarket_deposit_wallet_id,
)
from wayfinder_paths.core.utils.web3 import (
    _get_rpcs_for_chain_id,
    _wayfinder_auth_headers,
)

POLYGON_CHAIN_ID = 137
# keccak("predictWalletAddress(bytes32)")[:4] — the current factory's one-arg
# predict; a two-arg overload exists but ignores its address argument.
_PREDICT_SELECTOR = "0x04f1d3c7"
_BALANCE_OF_SELECTOR = "0x70a08231"

_RESOLVED: dict[str, str] = {}


def _predict_calldata(owner: str) -> str:
    return _PREDICT_SELECTOR + polymarket_deposit_wallet_id(owner).hex()


def _rpc_batch(calls: list[dict[str, Any]]) -> list[Any]:
    """POST one JSON-RPC batch to the first healthy Polygon RPC."""
    last_error: Exception | None = None
    payload = [{"jsonrpc": "2.0", "id": i, **call} for i, call in enumerate(calls)]
    for rpc_url in _get_rpcs_for_chain_id(POLYGON_CHAIN_ID):
        try:
            resp = httpx.post(
                rpc_url,
                json=payload,
                headers=_wayfinder_auth_headers(),
                timeout=10,
            )
            resp.raise_for_status()
            body = resp.json()
            by_id = {item["id"]: item for item in body}
            results = []
            for i in range(len(calls)):
                item = by_id.get(i)
                if item is None or "error" in item:
                    raise RuntimeError(f"rpc error: {item and item.get('error')}")
                results.append(item["result"])
            return results
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    raise RuntimeError(
        f"All Polygon RPCs failed while resolving the Polymarket deposit wallet: {last_error}"
    )


def resolve_deposit_wallet_sync(owner: str) -> str:
    """Resolve the canonical deposit wallet for `owner`.

    First call per owner does one JSON-RPC round-trip, then the result is
    cached for the process. Raises on RPC failure — refusing local-derivation
    fallback is the point of this module.
    """
    owner = to_checksum_address(owner)
    cached = _RESOLVED.get(owner)
    if cached is not None:
        return cached

    legacy = derive_legacy_deposit_wallet(owner)
    code_result, predict_result = _rpc_batch(
        [
            {"method": "eth_getCode", "params": [legacy, "latest"]},
            {
                "method": "eth_call",
                "params": [
                    {
                        "to": POLYMARKET_DEPOSIT_WALLET_FACTORY,
                        "data": _predict_calldata(owner),
                    },
                    "latest",
                ],
            },
        ]
    )

    if code_result not in (None, "0x", "0x0"):
        resolved = legacy
    else:
        predicted_int = int(predict_result, 16)
        if predicted_int == 0:
            raise RuntimeError(
                "Polymarket factory predicted the zero address for the deposit wallet"
            )
        resolved = to_checksum_address(f"{predicted_int:040x}"[-40:])

    _RESOLVED[owner] = resolved
    return resolved


async def resolve_deposit_wallet(owner: str) -> str:
    return await asyncio.to_thread(resolve_deposit_wallet_sync, owner)


POLYMARKET_RECOVERY_DISCORD_URL = "https://discord.gg/aiwayfinder"


def _get_deposit_wallet_status_sync(owner: str) -> dict[str, Any]:
    """Full deposit-wallet state for `owner` — the agent-facing source of
    truth for the three user cohorts:

      - "legacy":  wallet deployed before the 2026-06-29 factory upgrade,
        canonical and fully operational — nothing to do;
      - "beacon" + no stranded funds: current scheme (deployed or deployed-on-
        first-use) — nothing to do;
      - "beacon" + stranded_legacy_funds: the user deposited to the retired
        legacy derivation after the upgrade; recovery goes through the
        Wayfinder Discord, and no funds must ever be sent there again.
    """
    owner = to_checksum_address(owner)
    resolved = resolve_deposit_wallet_sync(owner)
    legacy = derive_legacy_deposit_wallet(owner)

    if resolved == legacy:
        return {
            "resolved_address": resolved,
            "legacy_address": legacy,
            "scheme": "legacy",
            "deployed": True,
            "stranded_legacy_funds": None,
            "guidance": (
                "Pre-2026-06-29 deposit wallet, deployed and fully "
                "operational. No action needed."
            ),
        }

    balance_call = abi_encode(["address"], [legacy]).hex()
    code_result, pusd_raw, usdc_e_raw = _rpc_batch(
        [
            {"method": "eth_getCode", "params": [resolved, "latest"]},
            *(
                {
                    "method": "eth_call",
                    "params": [
                        {"to": token, "data": _BALANCE_OF_SELECTOR + balance_call},
                        "latest",
                    ],
                }
                for token in (POLYGON_P_USDC_PROXY_ADDRESS, POLYGON_USDC_E_ADDRESS)
            ),
        ]
    )
    deployed = code_result not in (None, "0x", "0x0")
    pusd = int(pusd_raw, 16) if pusd_raw not in (None, "0x") else 0
    usdc_e = int(usdc_e_raw, 16) if usdc_e_raw not in (None, "0x") else 0

    stranded: dict[str, Any] | None = None
    if pusd or usdc_e:
        stranded = {
            "legacy_address": legacy,
            "pusd_raw": pusd,
            "usdc_e_raw": usdc_e,
            "message": (
                f"Funds are stranded at the retired pre-2026-06-29 deposit "
                f"address {legacy}, which can no longer be deployed. Do NOT "
                f"send more funds there. Recovery help: "
                f"{POLYMARKET_RECOVERY_DISCORD_URL}"
            ),
        }

    if stranded:
        guidance = (
            f"URGENT: {(pusd + usdc_e) / 1_000_000} pUSD/USDC.e is stranded at "
            f"the retired legacy address {legacy}. Point the user to "
            f"{POLYMARKET_RECOVERY_DISCORD_URL} for recovery. New deposits go "
            f"to {resolved} and are safe — the wallet is deployed and "
            "verified before any transfer."
        )
    elif deployed:
        guidance = "Current-scheme deposit wallet, deployed and operational."
    else:
        guidance = (
            "Deposit wallet not yet deployed; it is deployed and verified "
            "automatically before the first deposit or trade."
        )

    return {
        "resolved_address": resolved,
        "legacy_address": legacy,
        "scheme": "beacon",
        "deployed": deployed,
        "stranded_legacy_funds": stranded,
        "guidance": guidance,
    }


async def get_deposit_wallet_status(owner: str) -> dict[str, Any]:
    return await asyncio.to_thread(_get_deposit_wallet_status_sync, owner)
