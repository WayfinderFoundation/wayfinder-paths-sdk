"""Definitive Flash resting token orders, via the Wayfinder backend relay.

The relay holds the Definitive integrator key and pins every call to a wallet
the caller owns; it never signs. The wallet signs each order (EIP-712) and
each cancel (EIP-191) itself — see DefinitiveAdapter.
"""

from __future__ import annotations

from typing import Any

import httpx

from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url

OPEN_ORDER_STATUSES = (
    "ORDER_STATUS_PENDING",
    "ORDER_STATUS_ACCEPTED",
    "ORDER_STATUS_PARTIALLY_FILLED",
)


class DefinitiveError(Exception):
    """A refusal from the relay or Definitive, with its human message."""


class DefinitiveClient(WayfinderClient):
    def _url(self, path: str) -> str:
        return f"{get_api_base_url()}/blockchain/definitive/{path}"

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._authed_request(method, self._url(path), **kwargs)
        except httpx.HTTPStatusError as exc:
            try:
                detail = exc.response.json().get("detail")
            except ValueError:
                detail = None
            raise DefinitiveError(
                detail or f"Definitive request failed ({exc.response.status_code})"
            ) from None
        return response.json()

    async def config(self) -> dict[str, Any]:
        return await self._call("GET", "config/")

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        return await self._call("POST", "quote/", json=request)

    async def submit(self, request: dict[str, Any]) -> str:
        data = await self._call("POST", "order/", json=request)
        return str(data["orderId"])

    async def list_orders(
        self, wallet: str, statuses: tuple[str, ...] = OPEN_ORDER_STATUSES
    ) -> list[dict[str, Any]]:
        params = {"wallet": wallet}
        if statuses:
            params["statuses"] = ",".join(statuses)
        data = await self._call("GET", "", params=params)
        return list(data.get("orders") or [])

    async def get_order(self, wallet: str, order_id: str) -> dict[str, Any]:
        return await self._call("GET", f"{order_id}/", params={"wallet": wallet})

    async def cancel(self, wallet: str, order_id: str, signature: str) -> None:
        await self._call(
            "POST",
            f"{order_id}/cancel/",
            json={"wallet": wallet, "userSignature": signature},
        )


DEFINITIVE_CLIENT = DefinitiveClient()
