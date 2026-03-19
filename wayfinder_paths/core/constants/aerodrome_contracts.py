from __future__ import annotations

from eth_utils import to_checksum_address

from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE

# ---------------------------------------------------------------------------
# Aerodrome: Base mainnet (chain_id=8453) core contracts
# ---------------------------------------------------------------------------
#
# Sources: aerodrome-finance/contracts deployments.

AERODROME_BY_CHAIN: dict[int, dict[str, str]] = {
    CHAIN_ID_BASE: {
        "aero": to_checksum_address("0x940181a94A35A4569E4529A3CDfB74e38FD98631"),
        "router": to_checksum_address("0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43"),
        "pool_factory": to_checksum_address(
            "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"
        ),
        "voter": to_checksum_address("0x16613524e02ad97eDfeF371bC883F2F5d6C480A5"),
        "voting_escrow": to_checksum_address(
            "0xeBf418Fe2512e7E6bd9b87a8F0f294aCDC67e6B4"
        ),
        "rewards_distributor": to_checksum_address(
            "0x227f65131A261548b057215bB1D5Ab2997964C7d"
        ),
        "minter": to_checksum_address("0xeB018363F0a9Af8f91F06FEe6613a751b2A33FE5"),
        "gauge_factory": to_checksum_address(
            "0x35f35cA5B132CaDf2916BaB57639128eAC5bbcb5"
        ),
        "voting_rewards_factory": to_checksum_address(
            "0x45cA74858C579E717ee29A86042E0d53B252B504"
        ),
        "managed_rewards_factory": to_checksum_address(
            "0xFdA1fb5A2a5B23638C7017950506a36dcFD2bDC3"
        ),
        "factory_registry": to_checksum_address(
            "0x5C3F18F06CC09CA1910767A34a20F771039E37C0"
        ),
        "forwarder": to_checksum_address("0x15e62707FCA7352fbE35F51a8D6b0F8066A05DCc"),
        "pool_implementation": to_checksum_address(
            "0xA4e46b4f701c62e14DF11B48dCe76A7d793CD6d7"
        ),
    }
}

