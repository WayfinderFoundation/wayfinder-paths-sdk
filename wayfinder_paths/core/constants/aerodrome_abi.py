ROUTER_ABI = [
    {
        "name": "defaultFactory",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}],
    },
    {
        "name": "factoryRegistry",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}],
    },
    {
        "name": "poolFor",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "tokenA", "type": "address"},
            {"name": "tokenB", "type": "address"},
            {"name": "stable", "type": "bool"},
            {"name": "_factory", "type": "address"},
        ],
        "outputs": [{"type": "address"}],
    },
    {
        "name": "getAmountsOut",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "amountIn", "type": "uint256"},
            {
                "name": "routes",
                "type": "tuple[]",
                "components": [
                    {"name": "from", "type": "address"},
                    {"name": "to", "type": "address"},
                    {"name": "stable", "type": "bool"},
                    {"name": "factory", "type": "address"},
                ],
            },
        ],
        "outputs": [{"name": "amounts", "type": "uint256[]"}],
    },
    {
        "name": "swapExactTokensForTokens",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "amountIn", "type": "uint256"},
            {"name": "amountOutMin", "type": "uint256"},
            {
                "name": "routes",
                "type": "tuple[]",
                "components": [
                    {"name": "from", "type": "address"},
                    {"name": "to", "type": "address"},
                    {"name": "stable", "type": "bool"},
                    {"name": "factory", "type": "address"},
                ],
            },
            {"name": "to", "type": "address"},
            {"name": "deadline", "type": "uint256"},
        ],
        "outputs": [{"name": "amounts", "type": "uint256[]"}],
    },
    {
        "name": "addLiquidity",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "tokenA", "type": "address"},
            {"name": "tokenB", "type": "address"},
            {"name": "stable", "type": "bool"},
            {"name": "amountADesired", "type": "uint256"},
            {"name": "amountBDesired", "type": "uint256"},
            {"name": "amountAMin", "type": "uint256"},
            {"name": "amountBMin", "type": "uint256"},
            {"name": "to", "type": "address"},
            {"name": "deadline", "type": "uint256"},
        ],
        "outputs": [
            {"name": "amountA", "type": "uint256"},
            {"name": "amountB", "type": "uint256"},
            {"name": "liquidity", "type": "uint256"},
        ],
    },
]

POOL_FACTORY_ABI = [
    {
        "name": "getPool",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "tokenA", "type": "address"},
            {"name": "tokenB", "type": "address"},
            {"name": "stable", "type": "bool"},
        ],
        "outputs": [{"type": "address"}],
    },
    {
        "name": "isPool",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "pool", "type": "address"}],
        "outputs": [{"type": "bool"}],
    },
]

VOTING_ESCROW_ABI = [
    {
        "name": "createLock",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "_value", "type": "uint256"},
            {"name": "_lockDuration", "type": "uint256"},
        ],
        "outputs": [{"type": "uint256"}],
    },
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "owner", "type": "address"}],
        "outputs": [{"type": "uint256"}],
    },
    {
        "name": "balanceOfNFT",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "_tokenId", "type": "uint256"}],
        "outputs": [{"type": "uint256"}],
    },
    {
        "name": "locked",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "_tokenId", "type": "uint256"}],
        "outputs": [
            {
                "type": "tuple",
                "components": [
                    {"name": "amount", "type": "int128"},
                    {"name": "end", "type": "uint256"},
                    {"name": "isPermanent", "type": "bool"},
                ],
            }
        ],
    },
    {
        "name": "ownerOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [{"type": "address"}],
    },
]

VOTER_ABI = [
    {
        "name": "vote",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "_tokenId", "type": "uint256"},
            {"name": "_poolVote", "type": "address[]"},
            {"name": "_weights", "type": "uint256[]"},
        ],
        "outputs": [],
    },
    {
        "name": "lastVoted",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [{"type": "uint256"}],
    },
    {
        "name": "usedWeights",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [{"type": "uint256"}],
    },
    {
        "name": "votes",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "tokenId", "type": "uint256"},
            {"name": "pool", "type": "address"},
        ],
        "outputs": [{"type": "uint256"}],
    },
    {
        "name": "gauges",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "pool", "type": "address"}],
        "outputs": [{"type": "address"}],
    },
]

GAUGE_ABI = [
    {
        "name": "stakingToken",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}],
    },
    {
        "name": "rewardToken",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}],
    },
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"type": "uint256"}],
    },
    {
        "name": "earned",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"type": "uint256"}],
    },
    {
        "name": "deposit",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "_amount", "type": "uint256"}],
        "outputs": [],
    },
]

SUGAR_ABI = [
    {
        "name": "all",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "_limit", "type": "uint256"},
            {"name": "_offset", "type": "uint256"},
        ],
        "outputs": [
            {
                "name": "",
                "type": "tuple[]",
                "components": [
                    {"name": "lp", "type": "address"},
                    {"name": "symbol", "type": "string"},
                    {"name": "decimals", "type": "uint8"},
                    {"name": "liquidity", "type": "uint256"},
                    {"name": "type", "type": "int24"},
                    {"name": "tick", "type": "int24"},
                    {"name": "sqrt_ratio", "type": "uint160"},
                    {"name": "token0", "type": "address"},
                    {"name": "reserve0", "type": "uint256"},
                    {"name": "staked0", "type": "uint256"},
                    {"name": "token1", "type": "address"},
                    {"name": "reserve1", "type": "uint256"},
                    {"name": "staked1", "type": "uint256"},
                    {"name": "gauge", "type": "address"},
                    {"name": "gauge_liquidity", "type": "uint256"},
                    {"name": "gauge_alive", "type": "bool"},
                    {"name": "fee", "type": "address"},
                    {"name": "bribe", "type": "address"},
                    {"name": "factory", "type": "address"},
                    {"name": "emissions", "type": "uint256"},
                    {"name": "emissions_token", "type": "address"},
                    {"name": "pool_fee", "type": "uint256"},
                    {"name": "unstaked_fee", "type": "uint256"},
                    {"name": "token0_fees", "type": "uint256"},
                    {"name": "token1_fees", "type": "uint256"},
                    {"name": "created_at", "type": "uint256"},
                ],
            }
        ],
    },
    {
        "name": "epochsLatest",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "_limit", "type": "uint256"},
            {"name": "_offset", "type": "uint256"},
        ],
        "outputs": [
            {
                "type": "tuple[]",
                "components": [
                    {"name": "ts", "type": "uint256"},
                    {"name": "lp", "type": "address"},
                    {"name": "votes", "type": "uint256"},
                    {"name": "emissions", "type": "uint256"},
                    {
                        "name": "bribes",
                        "type": "tuple[]",
                        "components": [
                            {"name": "token", "type": "address"},
                            {"name": "amount", "type": "uint256"},
                        ],
                    },
                    {
                        "name": "fees",
                        "type": "tuple[]",
                        "components": [
                            {"name": "token", "type": "address"},
                            {"name": "amount", "type": "uint256"},
                        ],
                    },
                ],
            }
        ],
    },
    {
        "name": "epochsByAddress",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "_limit", "type": "uint256"},
            {"name": "_offset", "type": "uint256"},
            {"name": "_address", "type": "address"},
        ],
        "outputs": [
            {
                "type": "tuple[]",
                "components": [
                    {"name": "ts", "type": "uint256"},
                    {"name": "lp", "type": "address"},
                    {"name": "votes", "type": "uint256"},
                    {"name": "emissions", "type": "uint256"},
                    {
                        "name": "bribes",
                        "type": "tuple[]",
                        "components": [
                            {"name": "token", "type": "address"},
                            {"name": "amount", "type": "uint256"},
                        ],
                    },
                    {
                        "name": "fees",
                        "type": "tuple[]",
                        "components": [
                            {"name": "token", "type": "address"},
                            {"name": "amount", "type": "uint256"},
                        ],
                    },
                ],
            }
        ],
    },
]

SLIPSTREAM_HELPER_ABI = [
    {
        "name": "getSqrtRatioAtTick",
        "type": "function",
        "stateMutability": "pure",
        "inputs": [{"name": "tick", "type": "int24"}],
        "outputs": [{"type": "uint160"}],
    },
    {
        "name": "getTickAtSqrtRatio",
        "type": "function",
        "stateMutability": "pure",
        "inputs": [{"name": "sqrtPriceX96", "type": "uint160"}],
        "outputs": [{"type": "int24"}],
    },
    {
        "name": "getLiquidityForAmounts",
        "type": "function",
        "stateMutability": "pure",
        "inputs": [
            {"name": "sqrtRatioX96", "type": "uint160"},
            {"name": "sqrtRatioAX96", "type": "uint160"},
            {"name": "sqrtRatioBX96", "type": "uint160"},
            {"name": "amount0", "type": "uint256"},
            {"name": "amount1", "type": "uint256"},
        ],
        "outputs": [{"type": "uint128"}],
    },
    {
        "name": "getAmountsForLiquidity",
        "type": "function",
        "stateMutability": "pure",
        "inputs": [
            {"name": "sqrtRatioX96", "type": "uint160"},
            {"name": "sqrtRatioAX96", "type": "uint160"},
            {"name": "sqrtRatioBX96", "type": "uint160"},
            {"name": "liquidity", "type": "uint128"},
        ],
        "outputs": [
            {"name": "amount0", "type": "uint256"},
            {"name": "amount1", "type": "uint256"},
        ],
    },
]

SLIPSTREAM_CLPOOL_ABI = [
    {
        "name": "slot0",
        "type": "function",
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
        "name": "liquidity",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint128"}],
    },
    {
        "name": "fee",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint24"}],
    },
    {
        "name": "unstakedFee",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint24"}],
    },
    {
        "name": "token0",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}],
    },
    {
        "name": "token1",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}],
    },
    {
        "name": "tickSpacing",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "int24"}],
    },
]

SLIPSTREAM_QUOTER_ABI = [
    {
        "name": "quoteExactInputSingle",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "tokenIn", "type": "address"},
            {"name": "tokenOut", "type": "address"},
            {"name": "tickSpacing", "type": "int24"},
            {"name": "amountIn", "type": "uint256"},
            {"name": "sqrtPriceLimitX96", "type": "uint160"},
        ],
        "outputs": [{"name": "amountOut", "type": "uint256"}],
    },
    {
        "name": "quoteExactInput",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "path", "type": "bytes"},
            {"name": "amountIn", "type": "uint256"},
        ],
        "outputs": [{"name": "amountOut", "type": "uint256"}],
    },
]

SLIPSTREAM_FACTORY_ABI = [
    {
        "name": "getPool",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "tokenA", "type": "address"},
            {"name": "tokenB", "type": "address"},
            {"name": "tickSpacing", "type": "int24"},
        ],
        "outputs": [{"type": "address"}],
    }
]

REWARDS_DISTRIBUTOR_ABI = [
    {
        "name": "claimable",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [{"type": "uint256"}],
    }
]

VOTING_REWARD_ABI = [
    {
        "name": "rewardsListLength",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint256"}],
    },
    {
        "name": "rewards",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": "uint256"}],
        "outputs": [{"type": "address"}],
    },
    {
        "name": "earned",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "token", "type": "address"},
            {"name": "tokenId", "type": "uint256"},
        ],
        "outputs": [{"type": "uint256"}],
    },
]

SLIPSTREAM_NFPM_ABI = [
    {
        "name": "ownerOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [{"type": "address"}],
    },
    {
        "name": "approve",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "tokenId", "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "name": "setApprovalForAll",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "approved", "type": "bool"},
        ],
        "outputs": [],
    },
    {
        "name": "mint",
        "type": "function",
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
        "name": "positions",
        "type": "function",
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
]

SLIPSTREAM_GAUGE_ABI = [
    {
        "name": "deposit",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [],
    },
    {
        "name": "withdraw",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [],
    },
    {
        "name": "earned",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "tokenId", "type": "uint256"},
        ],
        "outputs": [{"type": "uint256"}],
    },
    {
        "name": "getReward",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [],
    },
    {
        "name": "rewardToken",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}],
    },
    {
        "name": "rewardRate",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint256"}],
    },
    {
        "name": "periodFinish",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint256"}],
    },
    {
        "name": "pool",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}],
    },
    {
        "name": "nft",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}],
    },
]
