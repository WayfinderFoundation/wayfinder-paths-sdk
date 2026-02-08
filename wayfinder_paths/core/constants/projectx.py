"""ProjectX (HyperEVM Uniswap v3 fork) constants shared by adapter + strategy."""

from __future__ import annotations

import os
from typing import Any

from eth_utils import to_checksum_address

from wayfinder_paths.core.constants.chains import CHAIN_ID_HYPEREVM
from wayfinder_paths.core.constants.contracts import (
    HYPEREVM_WHYPE,
    PRJX_NPM,
    PRJX_ROUTER,
)

PRJX_FACTORY = to_checksum_address("0xFf7B3e8C00e57ea31477c32A5B52a58Eea47b072")
PRJX_QUOTER = to_checksum_address("0x239F11a7A3E08f2B8110D4CA9F6B95d4c8865258")
PRJX_WNATIVE = HYPEREVM_WHYPE

PROJECTX_CHAIN_ID = CHAIN_ID_HYPEREVM

THBILL_USDC_POOL = to_checksum_address("0x49dfe4bbbd4c664e921aa2cde7ba1fc553959bf5")
THBILL_TOKEN = to_checksum_address("0xfdd22ce6d1f66bc0ec89b20bf16ccb6670f55a5a")
USDC_TOKEN = to_checksum_address("0xb88339cb7199b77e23db6e890353e22632ba630f")
WHYPE_TOKEN = PRJX_WNATIVE

# Strategy metadata for THBILL<>USDC pool
THBILL_USDC_METADATA: dict[str, Any] = {
    "pool": THBILL_USDC_POOL,
    "points_program": "Theo",
    "band_bps": 40.0,  # +/-0.20% (0.40% total) default band width
    "token0": USDC_TOKEN,
    "token1": THBILL_TOKEN,
    "router": PRJX_ROUTER,
    "npm": PRJX_NPM,
    "factory": PRJX_FACTORY,
    "quoter": PRJX_QUOTER,
    "chain_id": PROJECTX_CHAIN_ID,
}

# Token-id helpers (used for strategy display and token adapter lookups)
ADDRESS_TO_TOKEN_ID: dict[str, str] = {
    USDC_TOKEN: "usd-coin-hyperevm",
    THBILL_TOKEN: "theo-short-duration-us-treasury-fund-hyperevm",
    WHYPE_TOKEN: "wrapped-hype-hyperevm",
}
TOKEN_ID_TO_ADDRESS: dict[str, str] = {v: k for k, v in ADDRESS_TO_TOKEN_ID.items()}

PRJX_POINTS_API_URL = "https://api.prjx.com/points/user"


def get_prjx_subgraph_url(config: dict[str, Any] | None = None) -> str | None:
    """Resolve the ProjectX subgraph URL from config or environment variables."""

    if config and isinstance(config, dict):
        for key in ("prjx_subgraph_url", "projectx_subgraph_url", "subgraph_url"):
            value = config.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        strategy_cfg = config.get("strategy")
        if isinstance(strategy_cfg, dict):
            for key in ("prjx_subgraph_url", "projectx_subgraph_url", "subgraph_url"):
                value = strategy_cfg.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()

    for env_key in ("PRJX_SUBGRAPH_URL", "GOLD_SKY_URL"):
        env_val = os.getenv(env_key)
        if env_val and env_val.strip():
            return env_val.strip()

    return None
