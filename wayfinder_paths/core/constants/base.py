DEFAULT_NATIVE_GAS_UNITS = 21000
GAS_BUFFER_MULTIPLIER = 1.1
ONE_GWEI = 1_000_000_000
SUGGESTED_PRIORITY_FEE_MULTIPLIER = 1.5
SUGGESTED_GAS_PRICE_MULTIPLIER = 1.5
MAX_BASE_FEE_GROWTH_MULTIPLIER = 2

DEFAULT_SLIPPAGE = 0.005

# Timeout constants (seconds)
# Base L2 (and some RPC providers) can occasionally take >2 minutes to index/return receipts,
# even if the transaction is eventually mined. A longer timeout reduces false negatives that
# can lead to unsafe retry behavior (nonce gaps, duplicate swaps, etc.).
DEFAULT_HTTP_TIMEOUT = 30.0  # HTTP client timeout
DEFAULT_TRANSACTION_TIMEOUT = 180  # Transaction receipt timeout (seconds)

ADAPTER_BALANCE = "BALANCE"
ADAPTER_BRAP = "BRAP"
ADAPTER_MOONWELL = "MOONWELL"
ADAPTER_HYPERLIQUID = "HYPERLIQUID"
ADAPTER_POOL = "POOL"
ADAPTER_TOKEN = "TOKEN"
ADAPTER_LEDGER = "LEDGER"
ADAPTER_HYPERLEND = "HYPERLEND"
ADAPTER_UNISWAP = "UNISWAP"
ADAPTER_CCXT = "CCXT"

DEFAULT_PAGINATION_LIMIT = 50

MANTISSA = 10**18
SECONDS_PER_YEAR = 365 * 24 * 60 * 60
MAX_UINT256 = 2**256 - 1

NATIVE_COINGECKO_IDS = {
    "ethereum",
    "polygon-ecosystem-token",
    "avalanche-2",
    "binancecoin",
    "hyperliquid",
    "plasma",
}

NATIVE_GAS_SYMBOLS = {"eth", "pol", "avax", "bnb", "hype", "xpl"}
