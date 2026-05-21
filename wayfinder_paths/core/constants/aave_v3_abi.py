from __future__ import annotations

from wayfinder_paths.core.constants.erc4626_abi import ERC4626_ABI

# Minimal ABIs for Aave v3 pool operations + UI periphery helpers.

POOL_ABI = [
    {
        "name": "supply",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "onBehalfOf", "type": "address"},
            {"name": "referralCode", "type": "uint16"},
        ],
        "outputs": [],
    },
    {
        "name": "withdraw",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "to", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "borrow",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "interestRateMode", "type": "uint256"},
            {"name": "referralCode", "type": "uint16"},
            {"name": "onBehalfOf", "type": "address"},
        ],
        "outputs": [],
    },
    {
        "name": "repay",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "interestRateMode", "type": "uint256"},
            {"name": "onBehalfOf", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "setUserUseReserveAsCollateral",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "useAsCollateral", "type": "bool"},
        ],
        "outputs": [],
    },
    {
        "name": "setUserEMode",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "categoryId", "type": "uint8"}],
        "outputs": [],
    },
    {
        "name": "getUserAccountData",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "user", "type": "address"}],
        "outputs": [
            {"name": "totalCollateralBase", "type": "uint256"},
            {"name": "totalDebtBase", "type": "uint256"},
            {"name": "availableBorrowsBase", "type": "uint256"},
            {"name": "currentLiquidationThreshold", "type": "uint256"},
            {"name": "ltv", "type": "uint256"},
            {"name": "healthFactor", "type": "uint256"},
        ],
    },
]


UI_POOL_DATA_PROVIDER_ABI = [
    {
        "name": "getReservesData",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "provider", "type": "address"}],
        "outputs": [
            {
                "name": "",
                "type": "tuple[]",
                "components": [
                    {"name": "underlyingAsset", "type": "address"},
                    {"name": "name", "type": "string"},
                    {"name": "symbol", "type": "string"},
                    {"name": "decimals", "type": "uint256"},
                    {"name": "baseLTVasCollateral", "type": "uint256"},
                    {"name": "reserveLiquidationThreshold", "type": "uint256"},
                    {"name": "reserveLiquidationBonus", "type": "uint256"},
                    {"name": "reserveFactor", "type": "uint256"},
                    {"name": "usageAsCollateralEnabled", "type": "bool"},
                    {"name": "borrowingEnabled", "type": "bool"},
                    {"name": "isActive", "type": "bool"},
                    {"name": "isFrozen", "type": "bool"},
                    {"name": "liquidityIndex", "type": "uint128"},
                    {"name": "variableBorrowIndex", "type": "uint128"},
                    {"name": "liquidityRate", "type": "uint128"},
                    {"name": "variableBorrowRate", "type": "uint128"},
                    {"name": "lastUpdateTimestamp", "type": "uint40"},
                    {"name": "aTokenAddress", "type": "address"},
                    {"name": "variableDebtTokenAddress", "type": "address"},
                    {"name": "interestRateStrategyAddress", "type": "address"},
                    {"name": "availableLiquidity", "type": "uint256"},
                    {"name": "totalScaledVariableDebt", "type": "uint256"},
                    {"name": "priceInMarketReferenceCurrency", "type": "uint256"},
                    {"name": "priceOracle", "type": "address"},
                    {"name": "variableRateSlope1", "type": "uint256"},
                    {"name": "variableRateSlope2", "type": "uint256"},
                    {"name": "baseVariableBorrowRate", "type": "uint256"},
                    {"name": "optimalUsageRatio", "type": "uint256"},
                    {"name": "isPaused", "type": "bool"},
                    {"name": "isSiloedBorrowing", "type": "bool"},
                    {"name": "accruedToTreasury", "type": "uint128"},
                    {"name": "unbacked", "type": "uint128"},
                    {"name": "isolationModeTotalDebt", "type": "uint128"},
                    {"name": "flashLoanEnabled", "type": "bool"},
                    {"name": "debtCeiling", "type": "uint256"},
                    {"name": "debtCeilingDecimals", "type": "uint256"},
                    {"name": "eModeCategoryId", "type": "uint8"},
                    {"name": "borrowCap", "type": "uint256"},
                    {"name": "supplyCap", "type": "uint256"},
                    {"name": "borrowableInIsolation", "type": "bool"},
                    {"name": "virtualAccActive", "type": "bool"},
                    {"name": "virtualUnderlyingBalance", "type": "uint128"},
                ],
            },
            {
                "name": "",
                "type": "tuple",
                "components": [
                    {"name": "marketReferenceCurrencyUnit", "type": "uint256"},
                    {"name": "marketReferenceCurrencyPriceInUsd", "type": "int256"},
                    {"name": "networkBaseTokenPriceInUsd", "type": "int256"},
                    {"name": "networkBaseTokenPriceDecimals", "type": "uint8"},
                ],
            },
        ],
    },
    {
        "name": "getUserReservesData",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "provider", "type": "address"},
            {"name": "user", "type": "address"},
        ],
        "outputs": [
            {
                "name": "",
                "type": "tuple[]",
                "components": [
                    {"name": "underlyingAsset", "type": "address"},
                    {"name": "scaledATokenBalance", "type": "uint256"},
                    {"name": "usageAsCollateralEnabledOnUser", "type": "bool"},
                    {"name": "scaledVariableDebt", "type": "uint256"},
                ],
            },
            {"name": "", "type": "uint8"},
        ],
    },
    {
        "name": "getEModes",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "provider", "type": "address"}],
        "outputs": [
            {
                "name": "",
                "type": "tuple[]",
                "components": [
                    {"name": "id", "type": "uint8"},
                    {
                        "name": "eMode",
                        "type": "tuple",
                        "components": [
                            {"name": "ltv", "type": "uint16"},
                            {"name": "liquidationThreshold", "type": "uint16"},
                            {"name": "liquidationBonus", "type": "uint16"},
                            {"name": "collateralBitmap", "type": "uint128"},
                            {"name": "label", "type": "string"},
                            {"name": "borrowableBitmap", "type": "uint128"},
                        ],
                    },
                ],
            }
        ],
    },
]

UI_POOL_RESERVE_KEYS = [
    c.get("name")
    for c in (
        UI_POOL_DATA_PROVIDER_ABI[0].get("outputs", [{}])[0].get("components") or []
    )
    if c.get("name")
]

UI_POOL_USER_RESERVE_KEYS = [
    c.get("name")
    for c in (
        UI_POOL_DATA_PROVIDER_ABI[1].get("outputs", [{}])[0].get("components") or []
    )
    if c.get("name")
]

UI_POOL_RESERVE_COMPONENTS_ORIGIN = [
    {"name": "underlyingAsset", "type": "address"},
    {"name": "name", "type": "string"},
    {"name": "symbol", "type": "string"},
    {"name": "decimals", "type": "uint256"},
    {"name": "baseLTVasCollateral", "type": "uint256"},
    {"name": "reserveLiquidationThreshold", "type": "uint256"},
    {"name": "reserveLiquidationBonus", "type": "uint256"},
    {"name": "reserveFactor", "type": "uint256"},
    {"name": "usageAsCollateralEnabled", "type": "bool"},
    {"name": "borrowingEnabled", "type": "bool"},
    {"name": "isActive", "type": "bool"},
    {"name": "isFrozen", "type": "bool"},
    {"name": "liquidityIndex", "type": "uint128"},
    {"name": "variableBorrowIndex", "type": "uint128"},
    {"name": "liquidityRate", "type": "uint128"},
    {"name": "variableBorrowRate", "type": "uint128"},
    {"name": "lastUpdateTimestamp", "type": "uint40"},
    {"name": "aTokenAddress", "type": "address"},
    {"name": "variableDebtTokenAddress", "type": "address"},
    {"name": "interestRateStrategyAddress", "type": "address"},
    {"name": "availableLiquidity", "type": "uint256"},
    {"name": "totalScaledVariableDebt", "type": "uint256"},
    {"name": "priceInMarketReferenceCurrency", "type": "uint256"},
    {"name": "priceOracle", "type": "address"},
    {"name": "variableRateSlope1", "type": "uint256"},
    {"name": "variableRateSlope2", "type": "uint256"},
    {"name": "baseVariableBorrowRate", "type": "uint256"},
    {"name": "optimalUsageRatio", "type": "uint256"},
    {"name": "isPaused", "type": "bool"},
    {"name": "isSiloedBorrowing", "type": "bool"},
    {"name": "accruedToTreasury", "type": "uint128"},
    {"name": "isolationModeTotalDebt", "type": "uint128"},
    {"name": "flashLoanEnabled", "type": "bool"},
    {"name": "debtCeiling", "type": "uint256"},
    {"name": "debtCeilingDecimals", "type": "uint256"},
    {"name": "borrowCap", "type": "uint256"},
    {"name": "supplyCap", "type": "uint256"},
    {"name": "borrowableInIsolation", "type": "bool"},
    {"name": "virtualUnderlyingBalance", "type": "uint128"},
    {"name": "deficit", "type": "uint128"},
]

UI_POOL_DATA_PROVIDER_ORIGIN_ABI = [
    {
        "name": "getReservesData",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "provider", "type": "address"}],
        "outputs": [
            {
                "name": "",
                "type": "tuple[]",
                "components": UI_POOL_RESERVE_COMPONENTS_ORIGIN,
            },
            UI_POOL_DATA_PROVIDER_ABI[0]["outputs"][1],
        ],
    },
    UI_POOL_DATA_PROVIDER_ABI[1],
    UI_POOL_DATA_PROVIDER_ABI[2],
]

UI_POOL_RESERVE_KEYS_ORIGIN = [c["name"] for c in UI_POOL_RESERVE_COMPONENTS_ORIGIN]


UI_POOL_RESERVE_COMPONENTS_LEGACY = [
    {"name": "underlyingAsset", "type": "address"},
    {"name": "name", "type": "string"},
    {"name": "symbol", "type": "string"},
    {"name": "decimals", "type": "uint256"},
    {"name": "baseLTVasCollateral", "type": "uint256"},
    {"name": "reserveLiquidationThreshold", "type": "uint256"},
    {"name": "reserveLiquidationBonus", "type": "uint256"},
    {"name": "reserveFactor", "type": "uint256"},
    {"name": "usageAsCollateralEnabled", "type": "bool"},
    {"name": "borrowingEnabled", "type": "bool"},
    {"name": "stableBorrowRateEnabled", "type": "bool"},
    {"name": "isActive", "type": "bool"},
    {"name": "isFrozen", "type": "bool"},
    {"name": "liquidityIndex", "type": "uint128"},
    {"name": "variableBorrowIndex", "type": "uint128"},
    {"name": "liquidityRate", "type": "uint128"},
    {"name": "variableBorrowRate", "type": "uint128"},
    {"name": "stableBorrowRate", "type": "uint128"},
    {"name": "lastUpdateTimestamp", "type": "uint40"},
    {"name": "aTokenAddress", "type": "address"},
    {"name": "stableDebtTokenAddress", "type": "address"},
    {"name": "variableDebtTokenAddress", "type": "address"},
    {"name": "interestRateStrategyAddress", "type": "address"},
    {"name": "availableLiquidity", "type": "uint256"},
    {"name": "totalPrincipalStableDebt", "type": "uint256"},
    {"name": "averageStableRate", "type": "uint256"},
    {"name": "stableDebtLastUpdateTimestamp", "type": "uint256"},
    {"name": "totalScaledVariableDebt", "type": "uint256"},
    {"name": "priceInMarketReferenceCurrency", "type": "uint256"},
    {"name": "priceOracle", "type": "address"},
    {"name": "variableRateSlope1", "type": "uint256"},
    {"name": "variableRateSlope2", "type": "uint256"},
    {"name": "stableRateSlope1", "type": "uint256"},
    {"name": "stableRateSlope2", "type": "uint256"},
    {"name": "baseStableBorrowRate", "type": "uint256"},
    {"name": "baseVariableBorrowRate", "type": "uint256"},
    {"name": "optimalUsageRatio", "type": "uint256"},
    {"name": "isPaused", "type": "bool"},
    {"name": "isSiloedBorrowing", "type": "bool"},
    {"name": "accruedToTreasury", "type": "uint128"},
    {"name": "unbacked", "type": "uint128"},
    {"name": "isolationModeTotalDebt", "type": "uint128"},
    {"name": "flashLoanEnabled", "type": "bool"},
    {"name": "debtCeiling", "type": "uint256"},
    {"name": "debtCeilingDecimals", "type": "uint256"},
    {"name": "eModeCategoryId", "type": "uint8"},
    {"name": "borrowCap", "type": "uint256"},
    {"name": "supplyCap", "type": "uint256"},
    {"name": "eModeLtv", "type": "uint16"},
    {"name": "eModeLiquidationThreshold", "type": "uint16"},
    {"name": "eModeLiquidationBonus", "type": "uint16"},
    {"name": "eModePriceSource", "type": "address"},
    {"name": "eModeLabel", "type": "string"},
    {"name": "borrowableInIsolation", "type": "bool"},
]

UI_POOL_USER_RESERVE_COMPONENTS_LEGACY = [
    {"name": "underlyingAsset", "type": "address"},
    {"name": "scaledATokenBalance", "type": "uint256"},
    {"name": "usageAsCollateralEnabledOnUser", "type": "bool"},
    {"name": "stableBorrowRate", "type": "uint256"},
    {"name": "scaledVariableDebt", "type": "uint256"},
    {"name": "principalStableDebt", "type": "uint256"},
    {"name": "stableBorrowLastUpdateTimestamp", "type": "uint256"},
]

UI_POOL_DATA_PROVIDER_LEGACY_ABI = [
    {
        "name": "getReservesData",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "provider", "type": "address"}],
        "outputs": [
            {
                "name": "",
                "type": "tuple[]",
                "components": UI_POOL_RESERVE_COMPONENTS_LEGACY,
            },
            UI_POOL_DATA_PROVIDER_ABI[0]["outputs"][1],
        ],
    },
    {
        "name": "getUserReservesData",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "provider", "type": "address"},
            {"name": "user", "type": "address"},
        ],
        "outputs": [
            {
                "name": "",
                "type": "tuple[]",
                "components": UI_POOL_USER_RESERVE_COMPONENTS_LEGACY,
            },
            {"name": "", "type": "uint8"},
        ],
    },
]

UI_POOL_RESERVE_KEYS_LEGACY = [c["name"] for c in UI_POOL_RESERVE_COMPONENTS_LEGACY]
UI_POOL_USER_RESERVE_KEYS_LEGACY = [
    c["name"] for c in UI_POOL_USER_RESERVE_COMPONENTS_LEGACY
]


UI_INCENTIVE_DATA_PROVIDER_V3_ABI = [
    {
        "name": "getReservesIncentivesData",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "provider", "type": "address"}],
        "outputs": [
            {
                "name": "",
                "type": "tuple[]",
                "components": [
                    {"name": "underlyingAsset", "type": "address"},
                    {
                        "name": "aIncentiveData",
                        "type": "tuple",
                        "components": [
                            {"name": "tokenAddress", "type": "address"},
                            {"name": "incentiveControllerAddress", "type": "address"},
                            {
                                "name": "rewardsTokenInformation",
                                "type": "tuple[]",
                                "components": [
                                    {"name": "rewardTokenSymbol", "type": "string"},
                                    {
                                        "name": "rewardTokenAddress",
                                        "type": "address",
                                    },
                                    {"name": "rewardOracleAddress", "type": "address"},
                                    {"name": "emissionPerSecond", "type": "uint256"},
                                    {
                                        "name": "incentivesLastUpdateTimestamp",
                                        "type": "uint256",
                                    },
                                    {
                                        "name": "tokenIncentivesIndex",
                                        "type": "uint256",
                                    },
                                    {"name": "emissionEndTimestamp", "type": "uint256"},
                                    {"name": "rewardPriceFeed", "type": "int256"},
                                    {"name": "rewardTokenDecimals", "type": "uint8"},
                                    {"name": "precision", "type": "uint8"},
                                    {"name": "priceFeedDecimals", "type": "uint8"},
                                ],
                            },
                        ],
                    },
                    {
                        "name": "vIncentiveData",
                        "type": "tuple",
                        "components": [
                            {"name": "tokenAddress", "type": "address"},
                            {"name": "incentiveControllerAddress", "type": "address"},
                            {
                                "name": "rewardsTokenInformation",
                                "type": "tuple[]",
                                "components": [
                                    {"name": "rewardTokenSymbol", "type": "string"},
                                    {
                                        "name": "rewardTokenAddress",
                                        "type": "address",
                                    },
                                    {"name": "rewardOracleAddress", "type": "address"},
                                    {"name": "emissionPerSecond", "type": "uint256"},
                                    {
                                        "name": "incentivesLastUpdateTimestamp",
                                        "type": "uint256",
                                    },
                                    {
                                        "name": "tokenIncentivesIndex",
                                        "type": "uint256",
                                    },
                                    {"name": "emissionEndTimestamp", "type": "uint256"},
                                    {"name": "rewardPriceFeed", "type": "int256"},
                                    {"name": "rewardTokenDecimals", "type": "uint8"},
                                    {"name": "precision", "type": "uint8"},
                                    {"name": "priceFeedDecimals", "type": "uint8"},
                                ],
                            },
                        ],
                    },
                ],
            }
        ],
    },
    {
        "name": "getUserReservesIncentivesData",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "provider", "type": "address"},
            {"name": "user", "type": "address"},
        ],
        "outputs": [
            {
                "name": "",
                "type": "tuple[]",
                "components": [
                    {"name": "underlyingAsset", "type": "address"},
                    {
                        "name": "aTokenIncentivesUserData",
                        "type": "tuple",
                        "components": [
                            {"name": "tokenAddress", "type": "address"},
                            {"name": "incentiveControllerAddress", "type": "address"},
                            {
                                "name": "userRewardsInformation",
                                "type": "tuple[]",
                                "components": [
                                    {"name": "rewardTokenSymbol", "type": "string"},
                                    {"name": "rewardOracleAddress", "type": "address"},
                                    {
                                        "name": "rewardTokenAddress",
                                        "type": "address",
                                    },
                                    {
                                        "name": "userUnclaimedRewards",
                                        "type": "uint256",
                                    },
                                    {
                                        "name": "tokenIncentivesUserIndex",
                                        "type": "uint256",
                                    },
                                    {"name": "rewardPriceFeed", "type": "int256"},
                                    {"name": "priceFeedDecimals", "type": "uint8"},
                                    {"name": "rewardTokenDecimals", "type": "uint8"},
                                ],
                            },
                        ],
                    },
                    {
                        "name": "vTokenIncentivesUserData",
                        "type": "tuple",
                        "components": [
                            {"name": "tokenAddress", "type": "address"},
                            {"name": "incentiveControllerAddress", "type": "address"},
                            {
                                "name": "userRewardsInformation",
                                "type": "tuple[]",
                                "components": [
                                    {"name": "rewardTokenSymbol", "type": "string"},
                                    {"name": "rewardOracleAddress", "type": "address"},
                                    {
                                        "name": "rewardTokenAddress",
                                        "type": "address",
                                    },
                                    {
                                        "name": "userUnclaimedRewards",
                                        "type": "uint256",
                                    },
                                    {
                                        "name": "tokenIncentivesUserIndex",
                                        "type": "uint256",
                                    },
                                    {"name": "rewardPriceFeed", "type": "int256"},
                                    {"name": "priceFeedDecimals", "type": "uint8"},
                                    {"name": "rewardTokenDecimals", "type": "uint8"},
                                ],
                            },
                        ],
                    },
                ],
            }
        ],
    },
]


REWARDS_CONTROLLER_ABI = [
    {
        "name": "claimAllRewards",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "assets", "type": "address[]"},
            {"name": "to", "type": "address"},
        ],
        "outputs": [
            {"name": "rewardsList", "type": "address[]"},
            {"name": "claimedAmounts", "type": "uint256[]"},
        ],
    },
    {
        "name": "claimRewards",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "assets", "type": "address[]"},
            {"name": "amount", "type": "uint256"},
            {"name": "to", "type": "address"},
            {"name": "reward", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]


WRAPPED_TOKEN_GATEWAY_V3_ABI = [
    {
        "type": "function",
        "stateMutability": "view",
        "name": "getWETHAddress",
        "inputs": [],
        "outputs": [{"type": "address"}],
    },
    {
        "type": "function",
        "stateMutability": "payable",
        "name": "depositETH",
        "inputs": [
            {"name": "pool", "type": "address"},
            {"name": "onBehalfOf", "type": "address"},
            {"name": "referralCode", "type": "uint16"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "withdrawETH",
        "inputs": [
            {"name": "pool", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "to", "type": "address"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "stateMutability": "payable",
        "name": "repayETH",
        "inputs": [
            {"name": "pool", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "rateMode", "type": "uint256"},
            {"name": "onBehalfOf", "type": "address"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "borrowETH",
        "inputs": [
            {"name": "pool", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "interestRateMode", "type": "uint256"},
            {"name": "referralCode", "type": "uint16"},
        ],
        "outputs": [],
    },
]


WETH_ABI = [
    {
        "type": "function",
        "stateMutability": "payable",
        "name": "deposit",
        "inputs": [],
        "outputs": [],
    },
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "withdraw",
        "inputs": [{"name": "amount", "type": "uint256"}],
        "outputs": [],
    },
]


CHAINLINK_AGGREGATOR_ABI = [
    {
        "type": "function",
        "stateMutability": "view",
        "name": "decimals",
        "inputs": [],
        "outputs": [{"type": "uint8"}],
    },
    {
        "type": "function",
        "stateMutability": "view",
        "name": "latestAnswer",
        "inputs": [],
        "outputs": [{"type": "int256"}],
    },
]


AAVE_EARN_VAULT_ABI = [
    *ERC4626_ABI,
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "depositATokens",
        "inputs": [
            {"name": "assets", "type": "uint256"},
            {"name": "receiver", "type": "address"},
        ],
        "outputs": [{"name": "shares", "type": "uint256"}],
    },
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "withdrawATokens",
        "inputs": [
            {"name": "assets", "type": "uint256"},
            {"name": "receiver", "type": "address"},
            {"name": "owner", "type": "address"},
        ],
        "outputs": [{"name": "shares", "type": "uint256"}],
    },
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "mintWithATokens",
        "inputs": [
            {"name": "shares", "type": "uint256"},
            {"name": "receiver", "type": "address"},
        ],
        "outputs": [{"name": "assets", "type": "uint256"}],
    },
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "redeemAsATokens",
        "inputs": [
            {"name": "shares", "type": "uint256"},
            {"name": "receiver", "type": "address"},
            {"name": "owner", "type": "address"},
        ],
        "outputs": [{"name": "assets", "type": "uint256"}],
    },
    {
        "type": "function",
        "stateMutability": "view",
        "name": "getClaimableFees",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "stateMutability": "view",
        "name": "getLastVaultBalance",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "stateMutability": "view",
        "name": "getFee",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]
