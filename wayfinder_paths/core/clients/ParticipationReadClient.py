"""Read-only, fixed-origin clients for verified reward-program endpoints.

No signing, automatic registration, withdrawal or arbitrary URL entry point.
FLOP and PERPTools EVM participation remain blocked until their interfaces and
account attribution are verified; this client does not guess either one.
"""

from __future__ import annotations

import re
from typing import Any

import httpx


def _address(value: str) -> str:
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
        raise ValueError("expected an EVM account address")
    return value


class ParticipationReadClient:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None):
        self._http = httpx.AsyncClient(
            transport=transport, timeout=15, follow_redirects=False
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _get(
        self,
        url: str,
        *,
        token: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        response = await self._http.get(url, headers=headers, params=params)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or result.get("error") or result.get("code"):
            raise ValueError("protocol returned an error or invalid response")
        # RISEx uses a data envelope in deployed responses; IMD is unwrapped.
        data = result.get("data", result)
        if not isinstance(data, dict):
            raise ValueError("expected a protocol response object")
        return data

    async def risex_config(self) -> dict[str, Any]:
        data = await self._get("https://api.rise.trade/v1/system/config")
        if str((data.get("chain") or {}).get("chain_id")) != "4153":
            raise ValueError("RISEx mainnet chain identity mismatch")
        return data

    async def risex_points(self, account: str, *, token: str) -> dict[str, Any]:
        data = await self._get(
            f"https://api.rise.trade/v1/points/{_address(account)}", token=token
        )
        if str(data.get("wallet_address") or "").lower() != account.lower():
            raise ValueError("RISEx points account mismatch")
        return data

    async def risex_points_history(self, account: str, *, token: str) -> dict[str, Any]:
        data = await self._get(
            f"https://api.rise.trade/v1/points/{_address(account)}/history", token=token
        )
        for row in data.get("entries", []):
            if (
                str((row.get("ledger") or {}).get("wallet_address") or "").lower()
                != account.lower()
            ):
                raise ValueError("RISEx history account mismatch")
        return data

    async def risex_fees(self, *, token: str) -> dict[str, Any]:
        return await self._get("https://api.rise.trade/v1/user/fees", token=token)

    async def imd_version(self) -> dict[str, Any]:
        return await self._get("https://api.imd.fun/version")

    async def imd_seat(self, token_id: int, *, account: str) -> dict[str, Any]:
        if token_id < 0:
            raise ValueError("seat ID must be nonnegative")
        data = await self._get(
            f"https://api.imd.fun/seats/{token_id}", params={"work": 0, "reviews": 0}
        )
        if str(data.get("owner") or "").lower() != _address(account).lower():
            raise ValueError("IMD seat does not belong to configured account")
        return data

    async def imd_earnings(self, account: str) -> dict[str, Any]:
        data = await self._get(
            f"https://api.imd.fun/wallets/{_address(account)}/earnings",
            params={"limit": 20},
        )
        if str(data.get("wallet") or "").lower() != account.lower():
            raise ValueError("IMD earnings account mismatch")
        return data
