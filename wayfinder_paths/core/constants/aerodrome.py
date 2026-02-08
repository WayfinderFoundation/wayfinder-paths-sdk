from eth_utils import to_checksum_address

# Aerodrome (Base) — core addresses
# Source: https://github.com/aerodrome-finance/contracts (Base deployments)

# Tokens
BASE_AERO = to_checksum_address("0x940181a94A35A4569E4529A3CDfB74e38FD98631")

# Core protocol contracts
AERODROME_VOTING_ESCROW = to_checksum_address(
    "0xeBf418Fe2512e7E6bd9b87a8F0f294aCDC67e6B4"
)
AERODROME_VOTER = to_checksum_address("0x16613524e02ad97eDfeF371bC883F2F5d6C480A5")
AERODROME_ROUTER = to_checksum_address("0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43")
AERODROME_REWARDS_DISTRIBUTOR = to_checksum_address(
    "0x227f65131A261548b057215bB1D5Ab2997964C7d"
)

# Sugar (onchain data helper)
AERODROME_SUGAR = to_checksum_address("0x68c19e13618C41158fE4bAba1B8fb3A9c74bDb0A")

# Aerodrome Slipstream (concentrated liquidity) — key helpers
# Source: Aerodrome published contract-address list (Base deployments)
AERODROME_SLIPSTREAM_HELPER = to_checksum_address(
    "0x0AD09A66af0154a84e86F761313d02d0abB6edd5"
)
AERODROME_SLIPSTREAM_FACTORY = to_checksum_address(
    "0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A"
)
AERODROME_SLIPSTREAM_QUOTER = to_checksum_address(
    "0x254cF9E1E6e233aa1AC962CB9B05b2cfeAaE15b0"
)
AERODROME_SLIPSTREAM_MIXED_QUOTER = to_checksum_address(
    "0x0A5aA5D3a4d28014f967Bf0f29EAA3FF9807D5c6"
)
AERODROME_SLIPSTREAM_NFPM = to_checksum_address(
    "0x827922686190790b37229fd06084350E74485b72"
)
