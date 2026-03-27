from __future__ import annotations

from typing import Any

AERODROME_SLIPSTREAM_NPM_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "balanceOf",
        "stateMutability": "view",
        "inputs": [{"name": "owner", "type": "address"}],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "tokenOfOwnerByIndex",
        "stateMutability": "view",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "index", "type": "uint256"},
        ],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "ownerOf",
        "stateMutability": "view",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [{"type": "address"}],
    },
    {
        "type": "function",
        "name": "getApproved",
        "stateMutability": "view",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [{"type": "address"}],
    },
    {
        "type": "function",
        "name": "isApprovedForAll",
        "stateMutability": "view",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "operator", "type": "address"},
        ],
        "outputs": [{"type": "bool"}],
    },
    {
        "type": "function",
        "name": "approve",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "tokenId", "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "setApprovalForAll",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "approved", "type": "bool"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "positions",
        "stateMutability": "view",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [
            {"name": "nonce", "type": "uint96"},
            {"name": "operator", "type": "address"},
            {"name": "token0", "type": "address"},
            {"name": "token1", "type": "address"},
            {"name": "tickSpacing", "type": "int24"},
            {"name": "tickLower", "type": "int24"},
            {"name": "tickUpper", "type": "int24"},
            {"name": "liquidity", "type": "uint128"},
            {"name": "feeGrowthInside0LastX128", "type": "uint256"},
            {"name": "feeGrowthInside1LastX128", "type": "uint256"},
            {"name": "tokensOwed0", "type": "uint128"},
            {"name": "tokensOwed1", "type": "uint128"},
        ],
    },
    {
        "type": "function",
        "name": "mint",
        "stateMutability": "payable",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "token0", "type": "address"},
                    {"name": "token1", "type": "address"},
                    {"name": "tickSpacing", "type": "int24"},
                    {"name": "tickLower", "type": "int24"},
                    {"name": "tickUpper", "type": "int24"},
                    {"name": "amount0Desired", "type": "uint256"},
                    {"name": "amount1Desired", "type": "uint256"},
                    {"name": "amount0Min", "type": "uint256"},
                    {"name": "amount1Min", "type": "uint256"},
                    {"name": "recipient", "type": "address"},
                    {"name": "deadline", "type": "uint256"},
                    {"name": "sqrtPriceX96", "type": "uint160"},
                ],
            }
        ],
        "outputs": [
            {"name": "tokenId", "type": "uint256"},
            {"name": "liquidity", "type": "uint128"},
            {"name": "amount0", "type": "uint256"},
            {"name": "amount1", "type": "uint256"},
        ],
    },
    {
        "type": "function",
        "name": "increaseLiquidity",
        "stateMutability": "payable",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "tokenId", "type": "uint256"},
                    {"name": "amount0Desired", "type": "uint256"},
                    {"name": "amount1Desired", "type": "uint256"},
                    {"name": "amount0Min", "type": "uint256"},
                    {"name": "amount1Min", "type": "uint256"},
                    {"name": "deadline", "type": "uint256"},
                ],
            }
        ],
        "outputs": [
            {"name": "liquidity", "type": "uint128"},
            {"name": "amount0", "type": "uint256"},
            {"name": "amount1", "type": "uint256"},
        ],
    },
    {
        "type": "function",
        "name": "decreaseLiquidity",
        "stateMutability": "payable",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "tokenId", "type": "uint256"},
                    {"name": "liquidity", "type": "uint128"},
                    {"name": "amount0Min", "type": "uint256"},
                    {"name": "amount1Min", "type": "uint256"},
                    {"name": "deadline", "type": "uint256"},
                ],
            }
        ],
        "outputs": [
            {"name": "amount0", "type": "uint256"},
            {"name": "amount1", "type": "uint256"},
        ],
    },
    {
        "type": "function",
        "name": "collect",
        "stateMutability": "payable",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "tokenId", "type": "uint256"},
                    {"name": "recipient", "type": "address"},
                    {"name": "amount0Max", "type": "uint128"},
                    {"name": "amount1Max", "type": "uint128"},
                ],
            }
        ],
        "outputs": [
            {"name": "amount0", "type": "uint256"},
            {"name": "amount1", "type": "uint256"},
        ],
    },
    {
        "type": "function",
        "name": "burn",
        "stateMutability": "payable",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [],
    },
]


AERODROME_SLIPSTREAM_CL_FACTORY_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "voter",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}],
    },
    {
        "type": "function",
        "name": "legacyCLFactory",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}],
    },
    {
        "type": "function",
        "name": "tickSpacingToFee",
        "stateMutability": "view",
        "inputs": [{"name": "tickSpacing", "type": "int24"}],
        "outputs": [{"type": "uint24"}],
    },
    {
        "type": "function",
        "name": "tickSpacings",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "int24[]"}],
    },
    {
        "type": "function",
        "name": "getPool",
        "stateMutability": "view",
        "inputs": [
            {"name": "tokenA", "type": "address"},
            {"name": "tokenB", "type": "address"},
            {"name": "tickSpacing", "type": "int24"},
        ],
        "outputs": [{"type": "address"}],
    },
    {
        "type": "function",
        "name": "allPools",
        "stateMutability": "view",
        "inputs": [{"name": "index", "type": "uint256"}],
        "outputs": [{"type": "address"}],
    },
    {
        "type": "function",
        "name": "allPoolsLength",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "isPool",
        "stateMutability": "view",
        "inputs": [{"name": "pool", "type": "address"}],
        "outputs": [{"type": "bool"}],
    },
    {
        "type": "function",
        "name": "getSwapFee",
        "stateMutability": "view",
        "inputs": [{"name": "pool", "type": "address"}],
        "outputs": [{"type": "uint24"}],
    },
    {
        "type": "function",
        "name": "getUnstakedFee",
        "stateMutability": "view",
        "inputs": [{"name": "pool", "type": "address"}],
        "outputs": [{"type": "uint24"}],
    },
]


AERODROME_SLIPSTREAM_CL_POOL_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "factory",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}],
    },
    {
        "type": "function",
        "name": "token0",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}],
    },
    {
        "type": "function",
        "name": "token1",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}],
    },
    {
        "type": "function",
        "name": "gauge",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}],
    },
    {
        "type": "function",
        "name": "nft",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}],
    },
    {
        "type": "function",
        "name": "tickSpacing",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "int24"}],
    },
    {
        "type": "function",
        "name": "slot0",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [
            {"name": "sqrtPriceX96", "type": "uint160"},
            {"name": "tick", "type": "int24"},
            {"name": "observationIndex", "type": "uint16"},
            {"name": "observationCardinality", "type": "uint16"},
            {"name": "observationCardinalityNext", "type": "uint16"},
            {"name": "unlocked", "type": "bool"},
        ],
    },
    {
        "type": "function",
        "name": "fee",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint24"}],
    },
    {
        "type": "function",
        "name": "unstakedFee",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint24"}],
    },
    {
        "type": "function",
        "name": "liquidity",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint128"}],
    },
    {
        "type": "function",
        "name": "stakedLiquidity",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint128"}],
    },
    {
        "type": "function",
        "name": "rewardRate",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "rewardReserve",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "periodFinish",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "lastUpdated",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint32"}],
    },
]


AERODROME_SLIPSTREAM_CL_GAUGE_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "nft",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}],
    },
    {
        "type": "function",
        "name": "voter",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}],
    },
    {
        "type": "function",
        "name": "pool",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}],
    },
    {
        "type": "function",
        "name": "gaugeFactory",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}],
    },
    {
        "type": "function",
        "name": "feesVotingReward",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}],
    },
    {
        "type": "function",
        "name": "rewardToken",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}],
    },
    {
        "type": "function",
        "name": "periodFinish",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "rewardRate",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "rewards",
        "stateMutability": "view",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "earned",
        "stateMutability": "view",
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "tokenId", "type": "uint256"},
        ],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "getReward",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "deposit",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "withdraw",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "stakedValues",
        "stateMutability": "view",
        "inputs": [{"name": "depositor", "type": "address"}],
        "outputs": [{"type": "uint256[]"}],
    },
    {
        "type": "function",
        "name": "stakedByIndex",
        "stateMutability": "view",
        "inputs": [
            {"name": "depositor", "type": "address"},
            {"name": "index", "type": "uint256"},
        ],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "stakedContains",
        "stateMutability": "view",
        "inputs": [
            {"name": "depositor", "type": "address"},
            {"name": "tokenId", "type": "uint256"},
        ],
        "outputs": [{"type": "bool"}],
    },
    {
        "type": "function",
        "name": "stakedLength",
        "stateMutability": "view",
        "inputs": [{"name": "depositor", "type": "address"}],
        "outputs": [{"type": "uint256"}],
    },
]
