from __future__ import annotations

from wayfinder_paths.core.constants.morpho_abi import MARKET_PARAMS_COMPONENTS

AUTHORIZATION_COMPONENTS = [
    {"name": "authorizer", "type": "address"},
    {"name": "authorized", "type": "address"},
    {"name": "isAuthorized", "type": "bool"},
    {"name": "nonce", "type": "uint256"},
    {"name": "deadline", "type": "uint256"},
]

SIGNATURE_COMPONENTS = [
    {"name": "v", "type": "uint8"},
    {"name": "r", "type": "bytes32"},
    {"name": "s", "type": "bytes32"},
]

WITHDRAWAL_COMPONENTS = [
    {
        "name": "marketParams",
        "type": "tuple",
        "components": MARKET_PARAMS_COMPONENTS,
    },
    {"name": "amount", "type": "uint128"},
]

PERMIT2_DETAILS_COMPONENTS = [
    {"name": "token", "type": "address"},
    {"name": "amount", "type": "uint160"},
    {"name": "expiration", "type": "uint48"},
    {"name": "nonce", "type": "uint48"},
]

PERMIT2_SINGLE_COMPONENTS = [
    {
        "name": "details",
        "type": "tuple",
        "components": PERMIT2_DETAILS_COMPONENTS,
    },
    {"name": "spender", "type": "address"},
    {"name": "sigDeadline", "type": "uint256"},
]

BUNDLER3_ABI = [
    {
        "type": "function",
        "stateMutability": "payable",
        "name": "multicall",
        "inputs": [{"name": "data", "type": "bytes[]"}],
        "outputs": [],
    },
    {
        "type": "function",
        "stateMutability": "payable",
        "name": "morphoBorrow",
        "inputs": [
            {
                "name": "marketParams",
                "type": "tuple",
                "components": MARKET_PARAMS_COMPONENTS,
            },
            {"name": "assets", "type": "uint256"},
            {"name": "shares", "type": "uint256"},
            {"name": "slippageAmount", "type": "uint256"},
            {"name": "receiver", "type": "address"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "stateMutability": "payable",
        "name": "reallocateTo",
        "inputs": [
            {"name": "publicAllocator", "type": "address"},
            {"name": "vault", "type": "address"},
            {"name": "value", "type": "uint256"},
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
    {
        "type": "function",
        "stateMutability": "payable",
        "name": "approve2",
        "inputs": [
            {
                "name": "permitSingle",
                "type": "tuple",
                "components": PERMIT2_SINGLE_COMPONENTS,
            },
            {"name": "signature", "type": "bytes"},
            {"name": "skipRevert", "type": "bool"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "stateMutability": "payable",
        "name": "transferFrom2",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "stateMutability": "payable",
        "name": "urdClaim",
        "inputs": [
            {"name": "distributor", "type": "address"},
            {"name": "account", "type": "address"},
            {"name": "reward", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "proof", "type": "bytes32[]"},
            {"name": "skipRevert", "type": "bool"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "stateMutability": "payable",
        "name": "morphoSetAuthorizationWithSig",
        "inputs": [
            {
                "name": "authorization",
                "type": "tuple",
                "components": AUTHORIZATION_COMPONENTS,
            },
            {
                "name": "signature",
                "type": "tuple",
                "components": SIGNATURE_COMPONENTS,
            },
            {"name": "skipRevert", "type": "bool"},
        ],
        "outputs": [],
    },
]
