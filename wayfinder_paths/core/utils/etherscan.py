from __future__ import annotations

import json
from typing import Any

import httpx

from wayfinder_paths.core.config import get_etherscan_api_key
from wayfinder_paths.core.constants.chains import CHAIN_EXPLORER_URLS, ETHERSCAN_V2_API_URL


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
    """Fetch verified contract ABI from Etherscan V2.

    Uses the unified endpoint (``api.etherscan.io/v2/api``) with a ``chainid`` query
    parameter so the same key can fetch ABIs across supported Etherscan networks.

    Raises:
        ValueError: When the API key is missing, the contract isn't verified, or the
            ABI payload is invalid.
        httpx.HTTPError: On network/HTTP issues.
    """
    key = str(api_key or get_etherscan_api_key() or "").strip()
    if not key:
        raise ValueError(
            "Etherscan API key required to fetch contract ABI. "
            "Set system.etherscan_api_key in config.json or ETHERSCAN_API_KEY env var."
        )

    params = {
        "chainid": str(int(chain_id)),
        "module": "contract",
        "action": "getabi",
        "address": str(contract_address).strip(),
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
