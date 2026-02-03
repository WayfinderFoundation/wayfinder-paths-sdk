MTOKEN_ABI = [
    {
        "name": "mint",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "mintAmount", "type": "uint256"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "redeem",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "redeemTokens", "type": "uint256"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "redeemUnderlying",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "redeemAmount", "type": "uint256"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "borrow",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "borrowAmount", "type": "uint256"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "repayBorrow",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "repayAmount", "type": "uint256"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "owner", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "balanceOfUnderlying",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "owner", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "borrowBalanceCurrent",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "borrowBalanceStored",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "exchangeRateCurrent",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "exchangeRateStored",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "underlying",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "name": "supplyRatePerTimestamp",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "borrowRatePerTimestamp",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "totalBorrows",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "totalSupply",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "getCash",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "accrueInterest",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "decimals",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
    },
]

COMPTROLLER_ABI = [
    {
        "name": "enterMarkets",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "mTokens", "type": "address[]"}],
        "outputs": [{"name": "", "type": "uint256[]"}],
    },
    {
        "name": "exitMarket",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "mTokenAddress", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "getAccountLiquidity",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [
            {"name": "error", "type": "uint256"},
            {"name": "liquidity", "type": "uint256"},
            {"name": "shortfall", "type": "uint256"},
        ],
    },
    {
        "name": "markets",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "mToken", "type": "address"}],
        "outputs": [
            {"name": "isListed", "type": "bool"},
            {"name": "collateralFactorMantissa", "type": "uint256"},
        ],
    },
    {
        "name": "checkMembership",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "mToken", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "getAssetsIn",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "address[]"}],
    },
    {
        "name": "getAllMarkets",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address[]"}],
    },
    {
        "name": "getHypotheticalAccountLiquidity",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "mTokenModify", "type": "address"},
            {"name": "redeemTokens", "type": "uint256"},
            {"name": "borrowAmount", "type": "uint256"},
        ],
        "outputs": [
            {"name": "error", "type": "uint256"},
            {"name": "liquidity", "type": "uint256"},
            {"name": "shortfall", "type": "uint256"},
        ],
    },
    {
        "name": "closeFactorMantissa",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "liquidationIncentiveMantissa",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "claimReward",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "holder", "type": "address"}],
        "outputs": [],
    },
]

REWARD_DISTRIBUTOR_ABI = [
    {
        "name": "claimReward",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [],
        "outputs": [],
    },
    {
        "name": "claimReward",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "holder", "type": "address"},
            {"name": "mTokens", "type": "address[]"},
        ],
        "outputs": [],
    },
    {
        "name": "rewardToken",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "name": "rewardAccrued",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "holder", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "getOutstandingRewardsForUser",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "user", "type": "address"}],
        "outputs": [
            {
                "name": "",
                "type": "tuple[]",
                "components": [
                    {"name": "mToken", "type": "address"},
                    {
                        "name": "rewards",
                        "type": "tuple[]",
                        "components": [
                            {"name": "rewardToken", "type": "address"},
                            {"name": "totalReward", "type": "uint256"},
                            {"name": "supplySide", "type": "uint256"},
                            {"name": "borrowSide", "type": "uint256"},
                        ],
                    },
                ],
            }
        ],
    },
    {
        "name": "getOutstandingRewardsForUser",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "mToken", "type": "address"},
            {"name": "user", "type": "address"},
        ],
        "outputs": [
            {
                "name": "",
                "type": "tuple[]",
                "components": [
                    {"name": "rewardToken", "type": "address"},
                    {"name": "totalReward", "type": "uint256"},
                    {"name": "supplySide", "type": "uint256"},
                    {"name": "borrowSide", "type": "uint256"},
                ],
            }
        ],
    },
    {
        "name": "getAllMarketConfigs",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "mToken", "type": "address"}],
        "outputs": [
            {
                "name": "",
                "type": "tuple[]",
                "components": [
                    {"name": "mToken", "type": "address"},
                    {"name": "rewardToken", "type": "address"},
                    {"name": "owner", "type": "address"},
                    {"name": "emissionCap", "type": "uint256"},
                    {"name": "supplyEmissionsPerSec", "type": "uint256"},
                    {"name": "borrowEmissionsPerSec", "type": "uint256"},
                    {"name": "supplyGlobalIndex", "type": "uint256"},
                    {"name": "borrowGlobalIndex", "type": "uint256"},
                    {"name": "endTime", "type": "uint256"},
                ],
            }
        ],
    },
]

WETH_ABI = [
    {
        "name": "deposit",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [],
        "outputs": [],
    },
    {
        "name": "withdraw",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "wad", "type": "uint256"}],
        "outputs": [],
    },
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]
