from __future__ import annotations

from eth_utils import to_checksum_address

from wayfinder_paths.core.constants.chains import (
    CHAIN_ID_ARBITRUM,
    CHAIN_ID_AVALANCHE,
    CHAIN_ID_BASE,
    CHAIN_ID_BSC,
    CHAIN_ID_ETHEREUM,
)

# ---------------------------------------------------------------------------
# ether.fi: key addresses
# ---------------------------------------------------------------------------
#
# Sources: ether.fi deployed contracts (core + cross-chain), weETH-cross-chain repo README.
#
# Notes:
# - Core liquid restaking (deposit -> eETH, wrap -> weETH, requestWithdraw -> NFT -> claim) is mainnet-only.
# - Cross-chain weETH addresses below are token-only (LayerZero OFT). Minting via L2 sync pools is not
#   implemented here (ABIs live in etherfi-protocol/weETH-cross-chain).

ETHERFI_BY_CHAIN: dict[int, dict[str, str]] = {
    CHAIN_ID_ETHEREUM: {
        "liquidity_pool": to_checksum_address(
            "0x308861A430be4cce5502d0A12724771Fc6DaF216"
        ),
        "eeth": to_checksum_address("0x35fA164735182de50811E8e2E824cFb9B6118ac2"),
        "weeth": to_checksum_address("0xCd5fE23C85820F7B72D0926FC9b05b43E359b7ee"),
        "withdraw_request_nft": to_checksum_address(
            "0x7d5706f6ef3f89b3951e23e557cdfbc3239d4e2c"
        ),
        "ethfi": to_checksum_address("0xFe0c30065B384F05761f15d0CC899D4F9F9Cc0eB"),
    }
}

# Chain IDs kept local (not all are globally supported in this SDK).
CHAIN_ID_OPTIMISM = 10
CHAIN_ID_SCROLL = 534352
CHAIN_ID_LINEA = 59144
CHAIN_ID_ZKSYNC_ERA = 324

WEETH_TOKEN_BY_CHAIN_ID: dict[int, str] = {
    CHAIN_ID_ETHEREUM: ETHERFI_BY_CHAIN[CHAIN_ID_ETHEREUM]["weeth"],
    CHAIN_ID_ARBITRUM: to_checksum_address(
        "0x35751007a407ca6FEFfE80b3cB397736D2cf4dbe"
    ),
    CHAIN_ID_AVALANCHE: to_checksum_address(
        "0xA3D68b74bF0528fdD07263c60d6488749044914b"
    ),
    CHAIN_ID_BASE: to_checksum_address("0x04C0599Ae5A44757c0af6F9eC3b93da8976c150A"),
    CHAIN_ID_OPTIMISM: to_checksum_address(
        "0x5A7fACB970D094B6C7FF1df0eA68D99E6e73CBFF"
    ),
    CHAIN_ID_SCROLL: to_checksum_address("0x01f0a31698C4d065659b9bdC21B3610292a1c506"),
    CHAIN_ID_LINEA: to_checksum_address("0x1Bf74C010E6320bab11e2e5A532b5AC15e0b8aA6"),
    CHAIN_ID_ZKSYNC_ERA: to_checksum_address(
        "0xc1fa6e2e8667d9be0ca938a54c7e0285e9df924a"
    ),
    CHAIN_ID_BSC: to_checksum_address("0x04C0599Ae5A44757c0af6F9eC3b93da8976c150A"),
}

WEETH_L2_SYNC_POOL_BY_CHAIN_ID: dict[int, str] = {
    CHAIN_ID_LINEA: to_checksum_address("0x823106E745A62D0C2FC4d27644c62aDE946D9CCa"),
    CHAIN_ID_BASE: to_checksum_address("0xc38e046dFDAdf15f7F56853674242888301208a5"),
    CHAIN_ID_SCROLL: to_checksum_address("0x750cf0fd3bc891D8D864B732BC4AD340096e5e68"),
}


def weeth_token_by_chain_id(chain_id: int) -> str:
    """Return the weETH token address for a given chain_id (token-only on L2s)."""
    addr = WEETH_TOKEN_BY_CHAIN_ID.get(int(chain_id))
    if not addr:
        raise ValueError(f"Unsupported weETH chain_id={chain_id}")
    return addr
