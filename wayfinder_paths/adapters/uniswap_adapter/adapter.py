from __future__ import annotations

from typing import Any

from eth_utils import to_checksum_address

from wayfinder_paths.adapters.uniswap_adapter.base import UniswapV3BaseAdapter
from wayfinder_paths.core.constants.contracts import UNISWAP_V3_FACTORY, UNISWAP_V3_NPM

SUPPORTED_CHAIN_IDS = set(UNISWAP_V3_NPM.keys())


class UniswapAdapter(UniswapV3BaseAdapter):
    adapter_type = "UNISWAP"

    def __init__(
        self,
        config: dict[str, Any],
        *,
        strategy_wallet_signing_callback=None,
    ) -> None:
        chain_id = int(config.get("chain_id", 8453))
        if chain_id not in SUPPORTED_CHAIN_IDS:
            raise ValueError(
                f"Unsupported chain_id {chain_id} for Uniswap V3. "
                f"Supported: {sorted(SUPPORTED_CHAIN_IDS)}"
            )

        wallet = (config or {}).get("strategy_wallet") or {}
        addr = wallet.get("address")
        if not addr:
            raise ValueError("strategy_wallet.address is required for UniswapAdapter")
        owner = to_checksum_address(str(addr))

        super().__init__(
            "uniswap_adapter",
            config,
            chain_id=chain_id,
            npm_address=UNISWAP_V3_NPM[chain_id],
            factory_address=UNISWAP_V3_FACTORY[chain_id],
            owner=owner,
            strategy_wallet_signing_callback=strategy_wallet_signing_callback,
        )
