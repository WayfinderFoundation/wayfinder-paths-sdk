"""Polymarket protocol constants.

These are intentionally small and focused on what the SDK needs for:
- Market discovery (Gamma)
- Orderbook / price history (CLOB)
- User positions / trades (Data API)
- Bridging helper endpoints (Bridge API)

Addresses are Polygon mainnet (chain_id=137).
"""

from __future__ import annotations

POLYMARKET_GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB_BASE_URL = "https://clob.polymarket.com"
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
# Used on Amoy testnet by the official client config; not required on Polygon mainnet.
POLYMARKET_RISK_ADAPTER_EXCHANGE_ADDRESS = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

POLYMARKET_APPROVAL_TARGETS: list[str] = [
    POLYMARKET_CTF_EXCHANGE_ADDRESS,
    POLYMARKET_NEG_RISK_CTF_EXCHANGE_ADDRESS,
]

# Some NegRisk markets pay out an adapter "collateral" token which must be unwrapped.
POLYMARKET_ADAPTER_COLLATERAL_ADDRESS = "0x3A3BD7bb9528E159577F7C2e685CC81A765002E2"

MAX_UINT256 = (1 << 256) - 1
ZERO32_STR = "0x" + "00" * 32
