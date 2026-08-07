from __future__ import annotations

from typing import Any

from eth_utils import to_checksum_address

from wayfinder_paths.core.constants.chains import (
    CHAIN_ID_ARBITRUM,
    CHAIN_ID_BASE,
    CHAIN_ID_ETHEREUM,
    CHAIN_ID_POLYGON,
)

# Compound III / Comet official live deployments.
#
# Notes:
# - Addresses are keyed by chain ID and must not be treated as globally unique.
# - The adapter uses the Comet proxy addresses as the canonical market entrypoints.

COMPOUND_COMET_BY_CHAIN: dict[int, dict[str, Any]] = {
    CHAIN_ID_ETHEREUM: {
        "chain_name": "ethereum",
        "configurator": to_checksum_address(
            "0x316f9708bB98af7dA9c68C1C3b5e79039cD336E3"
        ),
        "rewards": to_checksum_address("0x1B0e765F6224C21223AeA2af16c1C46E38885a40"),
        "bulker": to_checksum_address("0xa397a8C2086C554B531c02E29f3291c9704B00c7"),
        "markets": {
            "usdc": {
                "comet": to_checksum_address(
                    "0xc3d688B66703497DAA19211EEdff47f25384cdc3"
                )
            },
            "usds": {
                "comet": to_checksum_address(
                    "0x5D409e56D886231aDAf00c8775665AD0f9897b56"
                )
            },
            "usdt": {
                "comet": to_checksum_address(
                    "0x3Afdc9BCA9213A35503b077a6072F3D0d5AB0840"
                )
            },
            "wbtc": {
                "comet": to_checksum_address(
                    "0xe85Dc543813B8c2CFEaAc371517b925a166a9293"
                )
            },
            "weth": {
                "comet": to_checksum_address(
                    "0xA17581A9E3356d9A858b789D68B4d866e593aE94"
                )
            },
            "wsteth": {
                "comet": to_checksum_address(
                    "0x3D0bb1ccaB520A66e607822fC55BC921738fAFE3"
                ),
                "bulker": to_checksum_address(
                    "0x2c776041CCFe903071AF44aa147368a9c8EEA518"
                ),
            },
        },
    },
    CHAIN_ID_BASE: {
        "chain_name": "base",
        "configurator": to_checksum_address(
            "0x45939657d1CA34A8FA39A924B71D28Fe8431e581"
        ),
        "rewards": to_checksum_address("0x123964802e6ABabBE1Bc9547D72Ef1B69B00A6b1"),
        "bulker": to_checksum_address("0x78D0677032A35c63D142a48A2037048871212a8C"),
        "markets": {
            "aero": {
                "comet": to_checksum_address(
                    "0x784efeB622244d2348d4F2522f8860B96fbEcE89"
                )
            },
            "usdbc": {
                "comet": to_checksum_address(
                    "0x9c4ec768c28520B50860ea7a15bd7213a9fF58bf"
                )
            },
            "usdc": {
                "comet": to_checksum_address(
                    "0xb125E6687d4313864e53df431d5425969c15Eb2F"
                )
            },
            "usds": {
                "comet": to_checksum_address(
                    "0x2c776041CCFe903071AF44aa147368a9c8EEA518"
                )
            },
            "weth": {
                "comet": to_checksum_address(
                    "0x46e6b214b524310239732D51387075E0e70970bf"
                )
            },
        },
    },
    CHAIN_ID_ARBITRUM: {
        "chain_name": "arbitrum",
        "configurator": to_checksum_address(
            "0xb21b06D71c75973babdE35b49fFDAc3F82Ad3775"
        ),
        "rewards": to_checksum_address("0x88730d254A2f7e6AC8388c3198aFd694bA9f7fae"),
        "bulker": to_checksum_address("0xbdE8F31D2DdDA895264e27DD990faB3DC87b372d"),
        "markets": {
            "usdc.e": {
                "comet": to_checksum_address(
                    "0xA5EDBDD9646f8dFF606d7448e414884C7d905dCA"
                )
            },
            "usdc": {
                "comet": to_checksum_address(
                    "0x9c4ec768c28520B50860ea7a15bd7213a9fF58bf"
                )
            },
            "usdt": {
                "comet": to_checksum_address(
                    "0xd98Be00b5D27fc98112BdE293e487f8D4cA57d07"
                )
            },
            "weth": {
                "comet": to_checksum_address(
                    "0x6f7D514bbD4aFf3BcD1140B7344b32f063dEe486"
                )
            },
        },
    },
    CHAIN_ID_POLYGON: {
        "chain_name": "polygon",
        "configurator": to_checksum_address(
            "0x83E0F742cAcBE66349E3701B171eE2487a26e738"
        ),
        "rewards": to_checksum_address("0x45939657d1CA34A8FA39A924B71D28Fe8431e581"),
        "bulker": to_checksum_address("0x59e242D352ae13166B4987aE5c990C232f7f7CD6"),
        "markets": {
            "usdc": {
                "comet": to_checksum_address(
                    "0xF25212E676D1F7F89Cd72fFEe66158f541246445"
                )
            },
            "usdt": {
                "comet": to_checksum_address(
                    "0xaeB318360f27748Acb200CE616E389A6C9409a07"
                )
            },
        },
    },
}
