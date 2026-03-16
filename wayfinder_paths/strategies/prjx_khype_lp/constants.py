from wayfinder_paths.core.constants.contracts import (
    HYPEREVM_WHYPE,
    KHYPE_ADDRESS,
    KHYPE_STAKING_ACCOUNTANT,
    PRJX_NPM,
    PRJX_ROUTER,
)

# Re-export addresses for use by strategy module
__all__ = [
    "KHYPE_STAKING_ACCOUNTANT",
    "PRJX_NPM",
    "PRJX_ROUTER",
]

# ─────────────────────────────────────────────────────────────────────────────
# TOKEN IDS (wayfinder token identifiers)
# ─────────────────────────────────────────────────────────────────────────────

HYPE_NATIVE = "hype-hyperevm"
WHYPE_TOKEN_ID = "wrapped-hype-hyperevm"
KHYPE_TOKEN_ID = "kinetic-staked-hype-hyperevm"

# Chain ID
HYPEREVM_CHAIN_ID = 999

# ─────────────────────────────────────────────────────────────────────────────
# TOKEN ORDERING
# ─────────────────────────────────────────────────────────────────────────────
# WHYPE (0x5555...) < kHYPE (0xfD73...) by address, so:
#   token0 = WHYPE, token1 = kHYPE
# Price in the pool is token1/token0 = kHYPE-per-WHYPE (or equivalently HYPE-per-kHYPE inverted).
# Since kHYPE appreciates vs HYPE, 1 kHYPE > 1 HYPE, so price (kHYPE/WHYPE) < 1.

TOKEN0_ADDRESS = HYPEREVM_WHYPE  # WHYPE
TOKEN1_ADDRESS = KHYPE_ADDRESS  # kHYPE

# ─────────────────────────────────────────────────────────────────────────────
# POOL CONFIG
# ─────────────────────────────────────────────────────────────────────────────

POOL_FEE = 500  # 0.05% fee tier
POOL_TICK_SPACING = 10

# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY THRESHOLDS
# ─────────────────────────────────────────────────────────────────────────────

MIN_NET_DEPOSIT = 5.0  # Minimum deposit in HYPE
MIN_HYPE_GAS = 0.1  # Reserve for gas (native HYPE)
RANGE_WIDTH_TICKS = 200  # ±100 ticks from center
REBALANCE_TICK_DRIFT = 50  # Rebalance when within 50 ticks of edge
COMPOUND_MIN_FEES_USD = 1.0  # Minimum fees to bother compounding

MAX_UINT128 = (1 << 128) - 1

# ─────────────────────────────────────────────────────────────────────────────
# ABIs
# ─────────────────────────────────────────────────────────────────────────────

KHYPE_STAKING_ACCOUNTANT_ABI = [
    {
        "inputs": [
            {"internalType": "uint256", "name": "kHYPEAmount", "type": "uint256"}
        ],
        "name": "kHYPEToHYPE",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]

WHYPE_ABI = [
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
        "inputs": [{"name": "owner", "type": "address"}],
        "outputs": [{"type": "uint256"}],
    },
]

ERC20_APPROVE_ABI = [
    {
        "name": "approve",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"type": "bool"}],
    },
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "owner", "type": "address"}],
        "outputs": [{"type": "uint256"}],
    },
]

POOL_ABI = [
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
            {"name": "feeProtocol", "type": "uint8"},
            {"name": "unlocked", "type": "bool"},
        ],
    },
]

NPM_ABI = [
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
                    {"name": "fee", "type": "uint24"},
                    {"name": "tickLower", "type": "int24"},
                    {"name": "tickUpper", "type": "int24"},
                    {"name": "amount0Desired", "type": "uint256"},
                    {"name": "amount1Desired", "type": "uint256"},
                    {"name": "amount0Min", "type": "uint256"},
                    {"name": "amount1Min", "type": "uint256"},
                    {"name": "recipient", "type": "address"},
                    {"name": "deadline", "type": "uint256"},
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
        "name": "increaseLiquidity",
        "type": "function",
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
        "name": "decreaseLiquidity",
        "type": "function",
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
        "name": "collect",
        "type": "function",
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
        "name": "burn",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [],
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
            {"name": "fee", "type": "uint24"},
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
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "owner", "type": "address"}],
        "outputs": [{"type": "uint256"}],
    },
    {
        "name": "tokenOfOwnerByIndex",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "index", "type": "uint256"},
        ],
        "outputs": [{"type": "uint256"}],
    },
]
