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
        "name": "deposit",
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
        "outputs": [],
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
                    {
                        "name": "priceInMarketReferenceCurrency",
                        "type": "uint256",
                    },
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
    }
]

UI_POOL_RESERVE_KEYS = [
    c.get("name")
    for c in (
        UI_POOL_DATA_PROVIDER_ABI[0].get("outputs", [{}])[0].get("components") or []
    )
    if c.get("name")
]

PROTOCOL_DATA_PROVIDER_ABI = [
    {
        "type": "function",
        "stateMutability": "view",
        "name": "getReserveTokensAddresses",
        "inputs": [{"name": "asset", "type": "address"}],
        "outputs": [
            {"name": "aTokenAddress", "type": "address"},
            {"name": "stableDebtTokenAddress", "type": "address"},
            {"name": "variableDebtTokenAddress", "type": "address"},
        ],
    },
    {
        "type": "function",
        "stateMutability": "view",
        "name": "getUserReserveData",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "user", "type": "address"},
        ],
        "outputs": [
            {"name": "currentATokenBalance", "type": "uint256"},
            {"name": "currentStableDebt", "type": "uint256"},
            {"name": "currentVariableDebt", "type": "uint256"},
            {"name": "liquidityRate", "type": "uint256"},
            {"name": "stableBorrowRate", "type": "uint256"},
            {"name": "variableBorrowRate", "type": "uint256"},
            {"name": "liquidityIndex", "type": "uint256"},
            {"name": "healthFactor", "type": "uint256"},
        ],
    },
]

WRAPPED_TOKEN_GATEWAY_ABI = [
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
            {"type": "address"},
            {"type": "address"},
            {"type": "uint16"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "withdrawETH",
        "inputs": [
            {"type": "address"},
            {"type": "uint256"},
            {"type": "address"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "stateMutability": "payable",
        "name": "repayETH",
        "inputs": [
            {"type": "address"},
            {"type": "uint256"},
            {"type": "address"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "borrowETH",
        "inputs": [
            {"type": "address"},
            {"type": "uint256"},
            {"type": "uint16"},
        ],
        "outputs": [],
    },
]

WETH_ABI = [
    {
        "inputs": [],
        "name": "deposit",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [{"name": "wad", "type": "uint256"}],
        "name": "withdraw",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]
