from __future__ import annotations

from typing import Any

import httpx
from loguru import logger

from wayfinder_paths.core.config import get_api_key, get_gorlami_base_url
from wayfinder_paths.core.constants.base import DEFAULT_HTTP_TIMEOUT


class GorlamiTestnetClient:
    def __init__(self):
        self.base_url = get_gorlami_base_url().rstrip("/")
        api_key = get_api_key()
        headers = {"X-API-KEY": api_key} if api_key else {}
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(DEFAULT_HTTP_TIMEOUT),
            headers=headers,
        )

    async def create_fork(self, chain_id: int) -> dict:
        url = f"{self.base_url}/fork"
        logger.debug(f"Creating fork for chain_id={chain_id}")

        resp = await self.client.post(url, params={"chainId": chain_id})
        resp.raise_for_status()

        data = resp.json()
        fork_id = data.get("fork_id") or data.get("forkId")
        if not fork_id:
            raise ValueError(f"Unexpected gorlami response: {data}")

        fork_info = {
            "fork_id": fork_id,
            # gorlami exposes JSON-RPC at POST /fork/{forkId}
            "rpc_url": f"{self.base_url}/fork/{fork_id}",
            "chain_id": chain_id,
        }
        logger.info(f"Created fork {fork_info['fork_id']} for chain {chain_id}")
        return fork_info

    async def delete_fork(self, fork_id: str) -> bool:
        url = f"{self.base_url}/fork/{fork_id}"
        logger.debug(f"Deleting fork {fork_id}")

        resp = await self.client.delete(url)
        if resp.status_code == 404:
            logger.warning(f"Fork {fork_id} not found (already deleted?)")
            return False

        resp.raise_for_status()
        logger.info(f"Deleted fork {fork_id}")
        return True

    async def send_rpc(self, fork_id: str, method: str, params: list) -> Any:
        url = f"{self.base_url}/fork/{fork_id}"
        payload = {
            "method": method,
            "params": params,
        }

        resp = await self.client.post(url, json=payload)
        resp.raise_for_status()

        data = resp.json()
        if "error" in data:
            raise Exception(f"RPC error: {data['error']}")
        return data.get("result")

    async def set_native_balance(self, fork_id: str, wallet: str, amount: int) -> bool:
        url = f"{self.base_url}/fork/{fork_id}/balance/native"
        payload = {
            "address": wallet,
            "balance": amount,
        }
        resp = await self.client.post(url, json=payload)
        resp.raise_for_status()
        logger.debug(
            f"Set native balance for {wallet} to {amount} wei on fork {fork_id}"
        )
        return True

    async def set_erc20_balance(
        self, fork_id: str, token: str, wallet: str, amount: int
    ) -> bool:
        url = f"{self.base_url}/fork/{fork_id}/balance/erc20"
        payload = {
            "address": wallet,
            "tokenAddress": token,
            "amount": amount,
        }

        resp = await self.client.post(url, json=payload)
        resp.raise_for_status()
        logger.debug(
            f"Set ERC20 balance for {wallet} token {token} to {amount} on fork {fork_id}"
        )
        return True

    async def close(self) -> None:
        await self.client.aclose()
