from __future__ import annotations

from typing import Any

from wayfinder_paths.core.constants.erc1155_abi import ERC1155_APPROVAL_ABI

POLYMARKET_GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB_BASE_URL = "https://clober2-630978094941.asia-northeast1.run.app"
POLYMARKET_DATA_BASE_URL = "https://data-api.polymarket.com"
POLYMARKET_BRIDGE_BASE_URL = "https://bridge.polymarket.com"

POLYGON_CHAIN_ID = 137

# Collateral
POLYGON_USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
POLYGON_USDC_ADDRESS = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"

# Polymarket contracts (CTF)
POLYMARKET_CONDITIONAL_TOKENS_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# Exchanges / operators that may require approvals depending on market type.
POLYMARKET_CTF_EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
POLYMARKET_NEG_RISK_CTF_EXCHANGE_ADDRESS = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
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
