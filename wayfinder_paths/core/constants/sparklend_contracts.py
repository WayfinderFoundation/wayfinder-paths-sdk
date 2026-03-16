from __future__ import annotations

from eth_utils import to_checksum_address

# SparkLend per-chain deployments.
#
# Sources:
# - Spark docs deployments page
# - Spark Address Registry (Pool, DataProvider, Incentives, WETH Gateway, Oracle)
#
# Notes:
# - Addresses are expected to be checksum addresses.
SPARKLEND_BY_CHAIN: dict[int, dict[str, str]] = {
    # Ethereum
    1: {
        "chain_name": "ethereum",
        "pool_addresses_provider_registry": to_checksum_address(
            "0x03cFa0C4622FF84E50E75062683F44c9587e6Cc1"
        ),
        "pool_addresses_provider": to_checksum_address(
            "0x02C3eA4e34C0cBd694D2adFa2c690EECbC1793eE"
        ),
        "pool": to_checksum_address("0xC13e21B648A5Ee794902342038FF3aDAB66BE987"),
        "pool_configurator": to_checksum_address(
            "0x542DBa469bdE58FAeE189ffB60C6b49CE60E0738"
        ),
        "protocol_data_provider": to_checksum_address(
            "0xFc21d6d146E6086B8359705C8b28512a983db0cb"
        ),
        "ui_pool_data_provider": to_checksum_address(
            "0xF028c2F4b19898718fD0F77b9b881CbfdAa5e8Bb"
        ),
        "ui_incentive_data_provider": to_checksum_address(
            "0xA7F8A757C4f7696c015B595F51B2901AC0121B18"
        ),
        "rewards_controller": to_checksum_address(
            "0x4370D3b6C9588E02ce9D22e684387859c7Ff5b34"
        ),
        "oracle": to_checksum_address("0x8105f69D9C41644c6A0803fDA7D03Aa70996cFD9"),
        "wrapped_native_gateway": to_checksum_address(
            "0xBD7D6a9ad7865463DE44B05F04559f65e3B11704"
        ),
        # Alias for AaveV3Adapter compatibility (Spark is Aave v3-based).
        "wrapped_token_gateway": to_checksum_address(
            "0xBD7D6a9ad7865463DE44B05F04559f65e3B11704"
        ),
        "acl_manager": to_checksum_address(
            "0xdA135Cd78A086025BcdC87B038a1C462032b510C"
        ),
        "emission_manager": to_checksum_address(
            "0xf09e48dd4CA8e76F63a57ADd428bB06fee7932a4"
        ),
        "treasury": to_checksum_address("0xb137E7d16564c81ae2b0C8ee6B55De81dd46ECe5"),
        "treasury_controller": to_checksum_address(
            "0x92eF091C5a1E01b3CE1ba0D0150C84412d818F7a"
        ),
        "dai_treasury": to_checksum_address(
            "0x856900aa78e856a5df1a2665eE3a66b2487cD68f"
        ),
    },
}
