from __future__ import annotations

from eth_utils import to_checksum_address

# Morpho Blue (Markets) deployments indexed by Morpho's public API.
# Source: https://api.morpho.org/graphql (publicAllocators / morphoBlues)
#
# Note: networks are included for convenience/debugging only.
MORPHO_BY_CHAIN: dict[int, dict[str, str]] = {
    1: {
        "network": "ethereum",
        "morpho": to_checksum_address("0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"),
        "public_allocator": to_checksum_address(
            "0xfd32fA2ca22c76dD6E550706Ad913FC6CE91c75D"
        ),
    },
    10: {
        "network": "Optimism",
        "morpho": to_checksum_address("0xce95AfbB8EA029495c66020883F87aaE8864AF92"),
        "public_allocator": to_checksum_address(
            "0x0d68a97324E602E02799CD83B42D337207B40658"
        ),
    },
    130: {
        "network": "Unichain",
        "morpho": to_checksum_address("0x8f5ae9CddB9f68de460C77730b018Ae7E04a140A"),
        "public_allocator": to_checksum_address(
            "0xB0c9a107fA17c779B3378210A7a593e88938C7C9"
        ),
    },
    137: {
        "network": "Polygon",
        "morpho": to_checksum_address("0x1bF0c2541F820E775182832f06c0B7Fc27A25f67"),
        "public_allocator": to_checksum_address(
            "0xfac15aff53ADd2ff80C2962127C434E8615Df0d3"
        ),
    },
    143: {
        "network": "Monad",
        "morpho": to_checksum_address("0xD5D960E8C380B724a48AC59E2DfF1b2CB4a1eAee"),
        "public_allocator": to_checksum_address(
            "0xfd70575B732F9482F4197FE1075492e114E97302"
        ),
    },
    988: {
        "network": "Stable",
        "morpho": to_checksum_address("0xa40103088A899514E3fe474cD3cc5bf811b1102e"),
        "public_allocator": to_checksum_address(
            "0xbCB063D4B6D479b209C186e462828CBACaC82DbE"
        ),
    },
    999: {
        "network": "Hyperliquid",
        "morpho": to_checksum_address("0x68e37dE8d93d3496ae143F2E900490f6280C57cD"),
        "public_allocator": to_checksum_address(
            "0x517505be22D9068687334e69ae7a02fC77edf4Fc"
        ),
    },
    8453: {
        "network": "base",
        "morpho": to_checksum_address("0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"),
        "public_allocator": to_checksum_address(
            "0xA090dD1a701408Df1d4d0B85b716c87565f90467"
        ),
    },
    42161: {
        "network": "Arbitrum",
        "morpho": to_checksum_address("0x6c247b1F6182318877311737BaC0844bAa518F5e"),
        "public_allocator": to_checksum_address(
            "0x769583Af5e9D03589F159EbEC31Cc2c23E8C355E"
        ),
    },
    98866: {
        "network": "Plume",
        "morpho": to_checksum_address("0x42b18785CE0Aed7BF7Ca43a39471ED4C0A3e0bB5"),
        "public_allocator": to_checksum_address(
            "0x58485338D93F4e3b4Bf2Af1C9f9C0aDF087AEf1C"
        ),
    },
    747474: {
        "network": "Katana",
        "morpho": to_checksum_address("0xD50F2DffFd62f94Ee4AEd9ca05C61d0753268aBc"),
        "public_allocator": to_checksum_address(
            "0x39EB6Da5e88194C82B13491Df2e8B3E213eD2412"
        ),
    },
}
