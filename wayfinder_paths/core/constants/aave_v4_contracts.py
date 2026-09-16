"""Arc mainnet deployment from bgd-labs/aave-address-book (AaveV4Arc)."""

from eth_utils import to_checksum_address

ARC_AAVE_V4_HUB = to_checksum_address("0x17288dfc86205301064577b98B02b81017e6F79C")
ARC_AAVE_V4_SPOKES = {
    "main": to_checksum_address("0xB843bdC3a87A05E77E07Df9FE48928b3A34b134d"),
    "forex": to_checksum_address("0x4164EBCAF74670aa74C8D4F59de6157c0780F1bB"),
}
ARC_AAVE_V4_VAULTS = {
    "USDC": to_checksum_address("0x42EAB64310E1D1c66b4d8aF7C9C4ce253885eB83"),
    "EURC": to_checksum_address("0x5A10b1533C0f1f181DC8a428BF5Eb58B08fc8d2c"),
    "cirBTC": to_checksum_address("0x83D364DbAf4e7018E0b87dB3FaB3d1d8535a6F13"),
    "WETH": to_checksum_address("0xe8B890fea6e1E3915A337eD3136487F2f4f7e59D"),
}
