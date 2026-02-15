from __future__ import annotations

from wayfinder_paths.core.constants.morpho_abi import MARKET_PARAMS_COMPONENTS

WITHDRAWAL_COMPONENTS = [
    {
        "name": "marketParams",
        "type": "tuple",
        "components": MARKET_PARAMS_COMPONENTS,
    },
    {"name": "amount", "type": "uint128"},
]

PUBLIC_ALLOCATOR_ABI = [
    {
        "type": "function",
        "stateMutability": "view",
        "name": "fee",
        "inputs": [{"name": "vault", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "stateMutability": "payable",
        "name": "reallocateTo",
        "inputs": [
            {"name": "vault", "type": "address"},
            {
                "name": "withdrawals",
                "type": "tuple[]",
                "components": WITHDRAWAL_COMPONENTS,
            },
            {
                "name": "supplyMarketParams",
                "type": "tuple",
                "components": MARKET_PARAMS_COMPONENTS,
            },
        ],
        "outputs": [],
    },
]

