from typing import Any

import httpx
from loguru import logger

from wayfinder_paths.core.config import get_gorlami_api_key, get_gorlami_base_url
from wayfinder_paths.core.constants.base import DEFAULT_HTTP_TIMEOUT


class GorlamiTestnetClient:
    def __init__(self):
        self.base_url = get_gorlami_base_url().rstrip("/")
        api_key = get_gorlami_api_key()
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
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
        fork_info = {
            "fork_id": data["fork_id"],
            "rpc_url": data["rpc_url"],
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
        url = f"{self.base_url}/rpc/{fork_id}"
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
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
        url = f"{self.base_url}/fork/{fork_id}/balance"
        payload = {
            "wallet": wallet,
            "amount": hex(amount),
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
        url = f"{self.base_url}/fork/{fork_id}/erc20"
        payload = {
            "token": token,
            "wallet": wallet,
            "amount": hex(amount),
        }

        resp = await self.client.post(url, json=payload)
        resp.raise_for_status()
        logger.debug(
            f"Set ERC20 balance for {wallet} token {token} to {amount} on fork {fork_id}"
        )
        return True

    async def close(self) -> None:
        await self.client.aclose()
