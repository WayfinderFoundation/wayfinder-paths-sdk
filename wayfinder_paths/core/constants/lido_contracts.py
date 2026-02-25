from __future__ import annotations

from eth_utils import to_checksum_address

from wayfinder_paths.core.constants.chains import CHAIN_ID_ETHEREUM

# Lido per-chain deployments.
#
# Deployed contracts:
# - https://docs.lido.fi/deployed-contracts/
#
# Notes:
# - Addresses are expected to be checksum addresses.
LIDO_BY_CHAIN: dict[int, dict[str, str]] = {
    CHAIN_ID_ETHEREUM: {
        "steth": to_checksum_address("0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84"),
        "wsteth": to_checksum_address("0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0"),
        "withdrawal_queue": to_checksum_address(
            "0x889edC2eDab5f40e902b864aD4d7AdE8E412F9B1"
        ),
    }
}

