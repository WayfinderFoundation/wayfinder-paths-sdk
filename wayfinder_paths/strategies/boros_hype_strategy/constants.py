from wayfinder_paths.core.constants.contracts import (
    HYPE_OFT_ADDRESS,
    KHYPE_ADDRESS,
    KHYPE_STAKING_ACCOUNTANT,
    LHYPE_ACCOUNTANT,
    LOOPED_HYPE_ADDRESS,
)
from wayfinder_paths.core.constants.contracts import (
    HYPEREVM_WHYPE as WHYPE_ADDRESS,
)

# Re-export addresses for use by strategy modules
__all__ = [
    "HYPE_OFT_ADDRESS",
    "WHYPE_ADDRESS",
    "KHYPE_ADDRESS",
    "KHYPE_STAKING_ACCOUNTANT",
    "LHYPE_ACCOUNTANT",
    "LOOPED_HYPE_ADDRESS",
]

# ─────────────────────────────────────────────────────────────────────────────
# TOKEN IDS (wayfinder token identifiers)
# ─────────────────────────────────────────────────────────────────────────────

# Arbitrum tokens
USDC_ARB = "usd-coin-arbitrum"
USDT_ARB = "usdt0-arbitrum"
ETH_ARB = "ethereum-arbitrum"

# HyperEVM tokens
HYPE_NATIVE = "hype-hyperevm"
WHYPE = "wrapped-hype-hyperevm"  # Wrapped HYPE (1:1 with native HYPE)
KHYPE_LST = "kinetic-staked-hype-hyperevm"
LOOPED_HYPE = "looped-hype-hyperevm"
USDC_HYPE = "usd-coin-hyperevm"

# Chain IDs
HYPEREVM_CHAIN_ID = 999
ARBITRUM_CHAIN_ID = 42161

# ABIs for exchange rate reads
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

LHYPE_ACCOUNTANT_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "quote", "type": "address"}],
        "name": "getRateInQuote",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]

# WHYPE contract ABI for unwrapping (standard WETH-like interface)
WHYPE_ABI = [
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


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY THRESHOLDS
# ─────────────────────────────────────────────────────────────────────────────

MIN_NET_DEPOSIT = 150.0  # Minimum deposit to activate strategy
MAX_HL_LEVERAGE = 2.0  # Maximum leverage on Hyperliquid shorts
PARTIAL_TRIM_THRESHOLD = 0.75  # Risk level to trigger partial trim
FULL_REBALANCE_THRESHOLD = 0.90  # Risk level to trigger full rebalance
ALLOCATION_DEVIATION_THRESHOLD = 0.03  # 3% deviation triggers rebalance
HORIZON_DAYS = 7  # Planning horizon in days


# ─────────────────────────────────────────────────────────────────────────────
# BOROS CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

# NOTE: Boros migrated the old USDT-collateralized HYPE market. The current
# entry path uses HYPE collateral (via the LayerZero OFT token on Arbitrum).
BOROS_HYPE_MARKET_ID = 51  # HYPERLIQUID-HYPE-27FEB2026 (fallback)
BOROS_HYPE_TOKEN_ID = 5  # HYPE collateral token ID on Boros (current)
# Boros requires a minimum cross-margin cash balance per token to place orders.
# For the HYPE collateral token (tokenId=5) this is currently 0.4 HYPE (MarketHub.getCashFeeData()).
BOROS_MIN_DEPOSIT_HYPE = 0.4
BOROS_MIN_TENOR_DAYS = 3  # Roll to new market if < 3 days to expiry
BOROS_ENABLE_MIN_TOTAL_USD = 80.0  # Skip Boros if capital below this

# LayerZero OFT bridge (HyperEVM native HYPE -> Arbitrum OFT HYPE)
# HYPE_OFT_ADDRESS imported from contracts.py
# ABI lives in `wayfinder_paths/core/constants/hype_oft_abi.py`.

# ─────────────────────────────────────────────────────────────────────────────
# GAS CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

MIN_HYPE_GAS = 0.1  # Minimum HYPE for HyperEVM gas
HYPE_DEPOSIT_REQUIREMENT = 0.1  # HYPE to ensure for gas on deposit


# ─────────────────────────────────────────────────────────────────────────────
# EXTERNAL API ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

LHYPE_API_URL = "https://app.loopingcollective.org/api/external/asset/lhype"
KHYPE_API_URL = "https://kinetiq.xyz/api/khype"
