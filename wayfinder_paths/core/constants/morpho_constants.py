from __future__ import annotations

from eth_utils import to_checksum_address

# Morpho Blue / Markets uses a 36-decimal oracle price scale for collateral pricing.
ORACLE_PRICE_SCALE = 10**36

# Merkl distributor used for Morpho Merkl program claims.
MERKL_DISTRIBUTOR_ADDRESS = to_checksum_address(
    "0x3Ef3D8bA38EBe18DB133cEc108f4D14CE00Dd9Ae"
)
