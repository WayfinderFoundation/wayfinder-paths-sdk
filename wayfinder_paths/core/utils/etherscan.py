from __future__ import annotations

import json
from typing import Any

import httpx

from wayfinder_paths.core.clients.ContractClient import CONTRACT_CLIENT
from wayfinder_paths.core.config import get_api_key, get_etherscan_api_key
from wayfinder_paths.core.constants.chains import (
    CHAIN_EXPLORER_URLS,
    ETHERSCAN_V2_API_URL,
)


def get_etherscan_transaction_link(chain_id: int, tx_hash: str) -> str | None:
    base_url = CHAIN_EXPLORER_URLS.get(chain_id)
    if not base_url:
        return None
    return f"{base_url}tx/{tx_hash}"


async def fetch_contract_abi(
    chain_id: int,
    contract_address: str,
    *,
    api_key: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    """Fetch a verified contract ABI.

    With a Wayfinder API key configured, the ABI comes from the Wayfinder API and
    no explorer key is needed locally. Otherwise this queries Etherscan V2
    directly (``api.etherscan.io/v2/api`` with a ``chainid`` parameter) using
    ``api_key`` or the configured Etherscan key.

    Raises:
        ValueError: When no key is available, the contract isn't verified, or the
            ABI payload is invalid.
        httpx.HTTPError: On network/HTTP issues.
    """
    address = str(contract_address).strip()
    if api_key is None and get_api_key():
        try:
            return await CONTRACT_CLIENT.get_abi(int(chain_id), address)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise ValueError("Contract source code not verified") from exc
            raise

    key = str(api_key or get_etherscan_api_key() or "").strip()
    if not key:
        raise ValueError(
            "API key required to fetch contract ABI. Configure a Wayfinder API key, "
            "or set system.etherscan_api_key in config.json or ETHERSCAN_API_KEY env var."
        )

    params = {
        "chainid": str(int(chain_id)),
        "module": "contract",
        "action": "getabi",
        "address": address,
        "apikey": key,
    }

    async def _fetch(c: httpx.AsyncClient) -> list[dict[str, Any]]:
        resp = await c.get(ETHERSCAN_V2_API_URL, params=params)
        resp.raise_for_status()
        try:
            data = resp.json()
        except Exception as exc:
            snippet = (resp.text or "").strip().replace("\n", " ")
            if len(snippet) > 200:
                snippet = snippet[:197] + "..."
            raise ValueError(
                f"Unexpected Etherscan ABI response (non-JSON): {snippet or '<empty>'}"
            ) from exc
        if not isinstance(data, dict):
            raise ValueError(f"Unexpected Etherscan ABI response: {data!r}")

        if str(data.get("status")) != "1":
            msg = (
                str(data.get("result") or "")
                or str(data.get("message") or "")
                or "Unknown error"
            ).strip()
            raise ValueError(msg or "Etherscan ABI request failed")

        raw = data.get("result")
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("Etherscan ABI response was empty")

        try:
            abi = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Failed to parse ABI JSON: {exc}") from exc

        if not isinstance(abi, list):
            raise ValueError("ABI payload is not a JSON array")

        return [i for i in abi if isinstance(i, dict)]

    if client is not None:
        return await _fetch(client)

    async with httpx.AsyncClient(timeout=30) as c:
        return await _fetch(c)
