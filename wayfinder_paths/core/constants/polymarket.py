from __future__ import annotations

from typing import Any

from wayfinder_paths.core.constants.erc1155_abi import ERC1155_APPROVAL_ABI

POLYMARKET_GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB_BASE_URL = "https://clob.polymarket.com"
# v2 test url before crossover. once crossover is complete, v2 will use the original url
# POLYMARKET_CLOB_BASE_URL = "https://clob-v2.polymarket.com"
POLYMARKET_DATA_BASE_URL = "https://data-api.polymarket.com"
POLYMARKET_BRIDGE_BASE_URL = "https://bridge.polymarket.com"

POLYGON_CHAIN_ID = 137

# Collateral
POLYGON_USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
POLYGON_USDC_ADDRESS = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
POLYGON_P_USDC_PROXY_ADDRESS = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
POLYGON_P_USDC_ADDRESS = "0x6bBCef9f7ef3B6C592c99e0f206a0DE94Ad0925f"

# Polymarket contracts (CTF)
POLYMARKET_CONDITIONAL_TOKENS_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
POLYMARKET_COLLATERAL_ONRAMP_ADDRESS = "0x93070a847efEf7F70739046A929D47a521F5B8ee"
POLYMARKET_COLLATERAL_OFFRAMP_ADDRESS = "0x2957922Eb93258b93368531d39fAcCA3B4dC5854"

# Exchanges / operators that may require approvals depending on market type.
# NOTE: If interacting with the contracts directly, use version 2 except for ClobAuthDomain 
# https://docs.polymarket.com/v2-migration#eip-712-domain
POLYMARKET_CTF_EXCHANGE_ADDRESS = "0xE111180000d2663C0091e4f400237545B87B996B"
POLYMARKET_NEG_RISK_CTF_EXCHANGE_ADDRESS = "0xe2222d279d744050d28e00520010520000310F59"
POLYMARKET_RISK_ADAPTER_EXCHANGE_ADDRESS = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

POLYMARKET_APPROVAL_TARGETS: list[str] = [
    POLYMARKET_CTF_EXCHANGE_ADDRESS,
    POLYMARKET_NEG_RISK_CTF_EXCHANGE_ADDRESS,
    POLYMARKET_RISK_ADAPTER_EXCHANGE_ADDRESS,
]

# Some NegRisk markets pay out an adapter "collateral" token which must be unwrapped.
POLYMARKET_ADAPTER_COLLATERAL_ADDRESS = "0x3A3BD7bb9528E159577F7C2e685CC81A765002E2"

MAX_UINT256 = (1 << 256) - 1
ZERO32_STR = "0x" + "00" * 32

CONDITIONAL_TOKENS_ABI: list[dict[str, Any]] = [
    *ERC1155_APPROVAL_ABI,
    {
        "type": "function",
        "stateMutability": "view",
        "name": "getCollectionId",
        "inputs": [
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSet", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bytes32"}],
    },
    {
        "type": "function",
        "stateMutability": "view",
        "name": "getPositionId",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "collectionId", "type": "bytes32"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "stateMutability": "view",
        "name": "balanceOf",
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "id", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "redeemPositions",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "outputs": [],
    },
]

TOKEN_UNWRAP_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "unwrap",
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [],
    }
]

POLYMARKET_COLLATERAL_RAMP_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "wrap",
        "inputs": [
            {"name": "_asset", "type": "address", "internalType": "address"},
            {"name": "_to", "type": "address", "internalType": "address"},
            {"name": "_amount", "type": "uint256", "internalType": "uint256"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "unwrap",
        "inputs": [
            {"name": "_asset", "type": "address", "internalType": "address"},
            {"name": "_to", "type": "address", "internalType": "address"},
            {"name": "_amount", "type": "uint256", "internalType": "uint256"},
        ],
        "outputs": [],
    },
]
