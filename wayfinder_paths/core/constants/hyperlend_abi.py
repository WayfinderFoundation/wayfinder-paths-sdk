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
