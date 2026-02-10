from typing import Any

import httpx
from eth_utils import to_checksum_address

from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter
from wayfinder_paths.core.clients.TokenClient import (
    TOKEN_CLIENT,
    GasToken,
    TokenDetails,
)
from wayfinder_paths.core.constants.chains import CHAIN_ID_TO_CODE
from wayfinder_paths.core.utils.tokens import get_erc20_metadata


class TokenAdapter(BaseAdapter):
    adapter_type: str = "TOKEN"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
    ):
        super().__init__("token_adapter", config)

    async def get_token_onchain(
        self, token_address: str, *, chain_id: int
    ) -> tuple[bool, TokenDetails | str]:
        try:
            symbol, name, decimals = await get_erc20_metadata(
                token_address, int(chain_id)
            )
            chain_code = CHAIN_ID_TO_CODE.get(int(chain_id), str(chain_id))
            data: dict[str, Any] = {
                "token_id": f"{chain_code}_{token_address}",
                "address": token_address,
                "symbol": symbol,
                "name": name,
                "decimals": int(decimals),
                "chain": {"id": int(chain_id)},
                "metadata": {"source": "onchain"},
            }
            return True, data  # type: ignore[return-value]
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def get_token(
        self, query: str, *, chain_id: int | None = None
    ) -> tuple[bool, TokenDetails | str]:
        try:
            data = await TOKEN_CLIENT.get_token_details(query, chain_id=chain_id)
            if data:
                return (True, data)
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 404:
                self.logger.error(f"Error getting token by query {query}: {e}")
                return (False, str(e))
            self.logger.warning(f"Could not find token with query {query}: {e}")
        except Exception as e:
            self.logger.error(f"Error getting token by query {query}: {e}")
            return (False, str(e))

        # API miss — try on-chain as last resort
        if chain_id is not None and query.startswith("0x"):
            return await self.get_token_onchain(
                to_checksum_address(query), chain_id=int(chain_id)
            )
        return (False, f"No token found for: {query}")

    async def get_token_price(
        self, token_id: str, *, chain_id: int | None = None
    ) -> tuple[bool, dict[str, Any] | str]:
        try:
            data = await TOKEN_CLIENT.get_token_details(
                token_id, market_data=True, chain_id=chain_id
            )
            if not data:
                return (False, f"No token found for: {token_id}")

            price_change_24h = data.get("price_change_24h", 0.0)
            price_data = {
                "current_price": data.get("current_price", 0.0),
                "price_change_24h": price_change_24h,
                "price_change_percentage_24h": data.get("price_change_percentage_24h")
                if data.get("price_change_percentage_24h") is not None
                else (float(price_change_24h) * 100.0 if price_change_24h else 0.0),
                "market_cap": data.get("market_cap", 0),
                "total_volume": data.get("total_volume_usd_24h", 0),
                "symbol": data.get("symbol", ""),
                "name": data.get("name", ""),
                "address": data.get("address", ""),
            }
            return (True, price_data)
        except Exception as e:
            self.logger.error(f"Error getting token price for {token_id}: {e}")
            return (False, str(e))

    async def get_amount_usd(
        self,
        token_id: str | None,
        raw_amount: int | str | None,
        decimals: int = 18,
    ) -> float | None:
        if raw_amount is None or token_id is None:
            return None
        success, price_data = await self.get_token_price(token_id)
        if not success or not isinstance(price_data, dict):
            return None
        price = price_data.get("current_price", 0.0)
        return price * float(raw_amount) / 10 ** int(decimals)

    async def get_gas_token(self, chain_code: str) -> tuple[bool, GasToken | str]:
        try:
            data = await TOKEN_CLIENT.get_gas_token(chain_code)
            if not data:
                return (False, f"No gas token found for chain: {chain_code}")
            return (True, data)
        except Exception as e:
            self.logger.error(f"Error getting gas token for chain {chain_code}: {e}")
            return (False, str(e))
