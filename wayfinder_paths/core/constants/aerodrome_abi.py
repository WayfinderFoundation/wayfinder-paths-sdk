from __future__ import annotations

from typing import Any

# Minimal ABIs for Aerodrome (Router / PoolFactory / Pool / Gauge / Voter / veAERO / Rewards).
#
# Source-of-truth: aerodrome-finance/contracts interfaces.


AERODROME_ROUTER_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "defaultFactory",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}],
    },
    {
        "type": "function",
        "name": "poolFor",
        "stateMutability": "view",
        "inputs": [
            {"name": "tokenA", "type": "address"},
            {"name": "tokenB", "type": "address"},
            {"name": "stable", "type": "bool"},
            {"name": "_factory", "type": "address"},
        ],
        "outputs": [{"name": "pool", "type": "address"}],
    },
    {
        "type": "function",
        "name": "quoteAddLiquidity",
        "stateMutability": "view",
        "inputs": [
            {"name": "tokenA", "type": "address"},
            {"name": "tokenB", "type": "address"},
            {"name": "stable", "type": "bool"},
            {"name": "_factory", "type": "address"},
            {"name": "amountADesired", "type": "uint256"},
            {"name": "amountBDesired", "type": "uint256"},
        ],
        "outputs": [
            {"name": "amountA", "type": "uint256"},
            {"name": "amountB", "type": "uint256"},
            {"name": "liquidity", "type": "uint256"},
        ],
    },
    {
        "type": "function",
        "name": "quoteRemoveLiquidity",
        "stateMutability": "view",
        "inputs": [
            {"name": "tokenA", "type": "address"},
            {"name": "tokenB", "type": "address"},
            {"name": "stable", "type": "bool"},
            {"name": "_factory", "type": "address"},
            {"name": "liquidity", "type": "uint256"},
        ],
        "outputs": [
            {"name": "amountA", "type": "uint256"},
            {"name": "amountB", "type": "uint256"},
        ],
    },
    {
        "type": "function",
        "name": "addLiquidity",
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
    {
        "type": "function",
        "name": "addLiquidityETH",
        "stateMutability": "payable",
        "inputs": [
            {"name": "token", "type": "address"},
            {"name": "stable", "type": "bool"},
            {"name": "amountTokenDesired", "type": "uint256"},
            {"name": "amountTokenMin", "type": "uint256"},
            {"name": "amountETHMin", "type": "uint256"},
            {"name": "to", "type": "address"},
            {"name": "deadline", "type": "uint256"},
        ],
        "outputs": [
            {"name": "amountToken", "type": "uint256"},
            {"name": "amountETH", "type": "uint256"},
            {"name": "liquidity", "type": "uint256"},
        ],
    },
    {
        "type": "function",
        "name": "removeLiquidity",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "tokenA", "type": "address"},
            {"name": "tokenB", "type": "address"},
            {"name": "stable", "type": "bool"},
            {"name": "liquidity", "type": "uint256"},
            {"name": "amountAMin", "type": "uint256"},
            {"name": "amountBMin", "type": "uint256"},
            {"name": "to", "type": "address"},
            {"name": "deadline", "type": "uint256"},
        ],
        "outputs": [
            {"name": "amountA", "type": "uint256"},
            {"name": "amountB", "type": "uint256"},
        ],
    },
    {
        "type": "function",
        "name": "removeLiquidityETH",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "token", "type": "address"},
            {"name": "stable", "type": "bool"},
            {"name": "liquidity", "type": "uint256"},
            {"name": "amountTokenMin", "type": "uint256"},
            {"name": "amountETHMin", "type": "uint256"},
            {"name": "to", "type": "address"},
            {"name": "deadline", "type": "uint256"},
        ],
        "outputs": [
            {"name": "amountToken", "type": "uint256"},
            {"name": "amountETH", "type": "uint256"},
        ],
    },
    {
        "type": "function",
        "name": "getAmountsOut",
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
]


AERODROME_POOL_FACTORY_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "getPool",
        "stateMutability": "view",
        "inputs": [
            {"name": "tokenA", "type": "address"},
            {"name": "tokenB", "type": "address"},
            {"name": "stable", "type": "bool"},
        ],
        "outputs": [{"name": "pool", "type": "address"}],
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
        "name": "allPoolsLength",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "getFee",
        "stateMutability": "view",
        "inputs": [
            {"name": "_pool", "type": "address"},
            {"name": "_stable", "type": "bool"},
        ],
        "outputs": [{"type": "uint256"}],
    },
]


AERODROME_POOL_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "metadata",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [
            {"name": "dec0", "type": "uint256"},
            {"name": "dec1", "type": "uint256"},
            {"name": "r0", "type": "uint256"},
            {"name": "r1", "type": "uint256"},
            {"name": "st", "type": "bool"},
            {"name": "t0", "type": "address"},
            {"name": "t1", "type": "address"},
        ],
    },
    {
        "type": "function",
        "name": "stable",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "bool"}],
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
        "name": "tokens",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}, {"type": "address"}],
    },
    {
        "type": "function",
        "name": "getReserves",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [
            {"name": "_reserve0", "type": "uint256"},
            {"name": "_reserve1", "type": "uint256"},
            {"name": "_blockTimestampLast", "type": "uint256"},
        ],
    },
    {
        "type": "function",
        "name": "claimable0",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "claimable1",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "claimFees",
        "stateMutability": "nonpayable",
        "inputs": [],
        "outputs": [{"type": "uint256"}, {"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "balanceOf",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "totalSupply",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint256"}],
    },
]


AERODROME_GAUGE_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "stakingToken",
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
        "name": "feesVotingReward",
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
        "name": "ve",
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
        "name": "totalSupply",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "balanceOf",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "earned",
        "stateMutability": "view",
        "inputs": [{"name": "_account", "type": "address"}],
        "outputs": [{"name": "_earned", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "getReward",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "_account", "type": "address"}],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "deposit",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "_amount", "type": "uint256"}],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "deposit",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "_amount", "type": "uint256"},
            {"name": "_recipient", "type": "address"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "withdraw",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "_amount", "type": "uint256"}],
        "outputs": [],
    },
]


AERODROME_VOTER_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "gauges",
        "stateMutability": "view",
        "inputs": [{"name": "pool", "type": "address"}],
        "outputs": [{"type": "address"}],
    },
    {
        "type": "function",
        "name": "poolForGauge",
        "stateMutability": "view",
        "inputs": [{"name": "gauge", "type": "address"}],
        "outputs": [{"type": "address"}],
    },
    {
        "type": "function",
        "name": "gaugeToFees",
        "stateMutability": "view",
        "inputs": [{"name": "gauge", "type": "address"}],
        "outputs": [{"type": "address"}],
    },
    {
        "type": "function",
        "name": "gaugeToBribe",
        "stateMutability": "view",
        "inputs": [{"name": "gauge", "type": "address"}],
        "outputs": [{"type": "address"}],
    },
    {
        "type": "function",
        "name": "isAlive",
        "stateMutability": "view",
        "inputs": [{"name": "gauge", "type": "address"}],
        "outputs": [{"type": "bool"}],
    },
    {
        "type": "function",
        "name": "isWhitelistedNFT",
        "stateMutability": "view",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [{"type": "bool"}],
    },
    {
        "type": "function",
        "name": "length",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint256"}],
    },
    # public array getter on Voter.sol
    {
        "type": "function",
        "name": "pools",
        "stateMutability": "view",
        "inputs": [{"name": "index", "type": "uint256"}],
        "outputs": [{"type": "address"}],
    },
    {
        "type": "function",
        "name": "votes",
        "stateMutability": "view",
        "inputs": [
            {"name": "tokenId", "type": "uint256"},
            {"name": "pool", "type": "address"},
        ],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "usedWeights",
        "stateMutability": "view",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "lastVoted",
        "stateMutability": "view",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "vote",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "_tokenId", "type": "uint256"},
            {"name": "_poolVote", "type": "address[]"},
            {"name": "_weights", "type": "uint256[]"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "reset",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "_tokenId", "type": "uint256"}],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "claimRewards",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "_gauges", "type": "address[]"}],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "claimBribes",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "_bribes", "type": "address[]"},
            {"name": "_tokens", "type": "address[][]"},
            {"name": "_tokenId", "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "claimFees",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "_fees", "type": "address[]"},
            {"name": "_tokens", "type": "address[][]"},
            {"name": "_tokenId", "type": "uint256"},
        ],
        "outputs": [],
    },
]


AERODROME_VOTING_ESCROW_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "token",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}],
    },
    {
        "type": "function",
        "name": "distributor",
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
        "name": "balanceOf",
        "stateMutability": "view",
        "inputs": [{"name": "owner", "type": "address"}],
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
        "name": "ownerToNFTokenIdList",
        "stateMutability": "view",
        "inputs": [
            {"name": "_owner", "type": "address"},
            {"name": "_index", "type": "uint256"},
        ],
        "outputs": [{"name": "_tokenId", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "balanceOfNFT",
        "stateMutability": "view",
        "inputs": [{"name": "_tokenId", "type": "uint256"}],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "voted",
        "stateMutability": "view",
        "inputs": [{"name": "_tokenId", "type": "uint256"}],
        "outputs": [{"type": "bool"}],
    },
    {
        "type": "function",
        "name": "createLock",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "_value", "type": "uint256"},
            {"name": "_lockDuration", "type": "uint256"},
        ],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "createLockFor",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "_value", "type": "uint256"},
            {"name": "_lockDuration", "type": "uint256"},
            {"name": "_to", "type": "address"},
        ],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "depositFor",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "_tokenId", "type": "uint256"},
            {"name": "_value", "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "increaseAmount",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "_tokenId", "type": "uint256"},
            {"name": "_value", "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "increaseUnlockTime",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "_tokenId", "type": "uint256"},
            {"name": "_lockDuration", "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "withdraw",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "_tokenId", "type": "uint256"}],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "lockPermanent",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "_tokenId", "type": "uint256"}],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "unlockPermanent",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "_tokenId", "type": "uint256"}],
        "outputs": [],
    },
]


AERODROME_REWARDS_DISTRIBUTOR_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "claimable",
        "stateMutability": "view",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "claim",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "claimMany",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "tokenIds", "type": "uint256[]"}],
        "outputs": [{"type": "bool"}],
    },
]


AERODROME_SUGAR_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "all",
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
        "type": "function",
        "name": "epochsLatest",
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
        "type": "function",
        "name": "epochsByAddress",
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


AERODROME_REWARD_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "rewardsListLength",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint256"}],
    },
    # public array getter on Reward.sol
    {
        "type": "function",
        "name": "rewards",
        "stateMutability": "view",
        "inputs": [{"name": "index", "type": "uint256"}],
        "outputs": [{"type": "address"}],
    },
    {
        "type": "function",
        "name": "earned",
        "stateMutability": "view",
        "inputs": [
            {"name": "token", "type": "address"},
            {"name": "tokenId", "type": "uint256"},
        ],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "getReward",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "tokenId", "type": "uint256"},
            {"name": "tokens", "type": "address[]"},
        ],
        "outputs": [],
    },
]
