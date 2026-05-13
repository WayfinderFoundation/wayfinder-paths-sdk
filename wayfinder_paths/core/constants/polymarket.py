from __future__ import annotations

from eth_abi import encode as abi_encode
from eth_utils import keccak, to_bytes, to_checksum_address

POLYMARKET_GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB_BASE_URL = "https://clob.polymarket.com"
# v2 test url before crossover. once crossover is complete, v2 will use the original url
# POLYMARKET_CLOB_BASE_URL = "https://clob-v2.polymarket.com"
POLYMARKET_DATA_BASE_URL = "https://data-api.polymarket.com"
POLYMARKET_BRIDGE_BASE_URL = "https://bridge.polymarket.com"
POLYMARKET_RELAYER_BASE_URL = "https://relayer-v2.polymarket.com"

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
POLYMARKET_CTF_EXCHANGE_ADDRESS = to_checksum_address(
    "0xE111180000d2663C0091e4f400237545B87B996B"
)
POLYMARKET_NEG_RISK_CTF_EXCHANGE_ADDRESS = to_checksum_address(
    "0xe2222d279d744050d28e00520010520000310F59"
)
POLYMARKET_RISK_ADAPTER_EXCHANGE_ADDRESS = to_checksum_address(
    "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
)

POLYMARKET_APPROVAL_TARGETS: list[str] = [
    POLYMARKET_CTF_EXCHANGE_ADDRESS,
    POLYMARKET_NEG_RISK_CTF_EXCHANGE_ADDRESS,
    POLYMARKET_RISK_ADAPTER_EXCHANGE_ADDRESS,
]

POLYMARKET_DEPOSIT_WALLET_FACTORY = "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07"
POLYMARKET_DEPOSIT_WALLET_IMPLEMENTATION = "0x58CA52ebe0DadfdF531Cde7062e76746de4Db1eB"
POLYMARKET_ERC1967_CONST1 = (
    "0xcc3735a920a3ca505d382bbc545af43d6000803e6038573d6000fd5b3d6000f3"
)
POLYMARKET_ERC1967_CONST2 = (
    "0x5155f3363d3d373d3d363d7f360894a13ba1a3210667c828492db98dca3e2076"
)
POLYMARKET_ERC1967_PREFIX = 0x61003D3D8160233D3973


def polymarket_deposit_wallet_id(owner: str) -> bytes:
    return to_bytes(hexstr=to_checksum_address(owner)).rjust(32, b"\x00")


def derive_deposit_wallet(owner: str) -> str:
    factory = to_checksum_address(POLYMARKET_DEPOSIT_WALLET_FACTORY)
    args = abi_encode(
        ["address", "bytes32"], [factory, polymarket_deposit_wallet_id(owner)]
    )
    n = len(args)
    combined = POLYMARKET_ERC1967_PREFIX + (n << 56)
    init_code = (
        combined.to_bytes(10, "big")
        + to_bytes(hexstr=to_checksum_address(POLYMARKET_DEPOSIT_WALLET_IMPLEMENTATION))
        + to_bytes(hexstr="0x6009")
        + to_bytes(hexstr=POLYMARKET_ERC1967_CONST2)
        + to_bytes(hexstr=POLYMARKET_ERC1967_CONST1)
        + args
    )
    raw = keccak(b"\xff" + to_bytes(hexstr=factory) + keccak(args) + keccak(init_code))
    return to_checksum_address(raw[-20:].hex())


# Some NegRisk markets pay out an adapter "collateral" token which must be unwrapped.
POLYMARKET_ADAPTER_COLLATERAL_ADDRESS = "0x3A3BD7bb9528E159577F7C2e685CC81A765002E2"

MAX_UINT256 = (1 << 256) - 1
ZERO32_STR = "0x" + "00" * 32
