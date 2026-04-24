from __future__ import annotations

from typing import Any

COMPOUND_COMET_BY_CHAIN: dict[int, dict[str, Any]] = {
    1: {
        "chain_name": "ethereum",
        "configurator": "0x316f9708bB98af7dA9c68C1C3b5e79039cD336E3",
        "rewards": "0x1B0e765F6224C21223AeA2af16c1C46E38885a40",
        "bulker": "0xa397a8C2086C554B531c02E29f3291c9704B00c7",
        "markets": {
            "usdc": {"comet": "0xc3d688B66703497DAA19211EEdff47f25384cdc3"},
            "usds": {"comet": "0x5D409e56D886231aDAf00c8775665AD0f9897b56"},
            "usdt": {"comet": "0x3Afdc9BCA9213A35503b077a6072F3D0d5AB0840"},
            "wbtc": {"comet": "0xe85Dc543813B8c2CFEaAc371517b925a166a9293"},
            "weth": {"comet": "0xA17581A9E3356d9A858b789D68B4d866e593aE94"},
            "wsteth": {
                "comet": "0x3D0bb1ccaB520A66e607822fC55BC921738fAFE3",
                "bulker": "0x2c776041CCFe903071AF44aa147368a9c8EEA518",
            },
        },
    },
    8453: {
        "chain_name": "base",
        "configurator": "0x45939657d1CA34A8FA39A924B71D28Fe8431e581",
        "rewards": "0x123964802e6ABabBE1Bc9547D72Ef1B69B00A6b1",
        "bulker": "0x78D0677032A35c63D142a48A2037048871212a8C",
        "markets": {
            "aero": {"comet": "0x784efeB622244d2348d4F2522f8860B96fbEcE89"},
            "usdbc": {"comet": "0x9c4ec768c28520B50860ea7a15bd7213a9fF58bf"},
            "usdc": {"comet": "0xb125E6687d4313864e53df431d5425969c15Eb2F"},
            "usds": {"comet": "0x2c776041CCFe903071AF44aa147368a9c8EEA518"},
            "weth": {"comet": "0x46e6b214b524310239732D51387075E0e70970bf"},
        },
    },
    42161: {
        "chain_name": "arbitrum",
        "configurator": "0xb21b06D71c75973babdE35b49fFDAc3F82Ad3775",
        "rewards": "0x88730d254A2f7e6AC8388c3198aFd694bA9f7fae",
        "bulker": "0xbdE8F31D2DdDA895264e27DD990faB3DC87b372d",
        "markets": {
            "usdc.e": {"comet": "0xA5EDBDD9646f8dFF606d7448e414884C7d905dCA"},
            "usdc": {"comet": "0x9c4ec768c28520B50860ea7a15bd7213a9fF58bf"},
            "usdt": {"comet": "0xd98Be00b5D27fc98112BdE293e487f8D4cA57d07"},
            "weth": {"comet": "0x6f7D514bbD4aFf3BcD1140B7344b32f063dEe486"},
        },
    },
    10: {
        "chain_name": "optimism",
        "configurator": "0x84E93EC6170ED630f5ebD89A1AAE72d4F63f2713",
        "rewards": "0x443EA0340cb75a160F31A440722dec7b5bc3C2E9",
        "bulker": "0xcb3643CC8294B23171272845473dEc49739d4Ba3",
        "markets": {
            "usdc": {"comet": "0x2e44e174f7D53F0212823acC11C01A11d58c5bCB"},
            "usdt": {"comet": "0x995E394b8B2437aC8Ce61Ee0bC610D617962B214"},
            "weth": {"comet": "0xE36A30D249f7761327fd973001A32010b521b6Fd"},
        },
    },
    137: {
        "chain_name": "polygon",
        "configurator": "0x83E0F742cAcBE66349E3701B171eE2487a26e738",
        "rewards": "0x45939657d1CA34A8FA39A924B71D28Fe8431e581",
        "bulker": "0x59e242D352ae13166B4987aE5c990C232f7f7CD6",
        "markets": {
            "usdc": {"comet": "0xF25212E676D1F7F89Cd72fFEe66158f541246445"},
            "usdt": {"comet": "0xaeB318360f27748Acb200CE616E389A6C9409a07"},
        },
    },
    534352: {
        "chain_name": "scroll",
        "configurator": "0xECAB0bEEa3e5DEa0c35d3E69468EAC20098032D7",
        "rewards": "0x70167D30964cbFDc315ECAe02441Af747bE0c5Ee",
        "bulker": "0x53C6D04e3EC7031105bAeA05B36cBc3C987C56fA",
        "markets": {
            "usdc": {"comet": "0xB2f97c1Bd3bf02f5e74d13f02E3e26F93D77CE44"},
        },
    },
    59144: {
        "chain_name": "linea",
        "configurator": "0x970FfD8E335B8fa4cd5c869c7caC3a90671d5Dc3",
        "rewards": "0x2c7118c4C88B9841FCF839074c26Ae8f035f2921",
        "bulker": "0x023ee795361B28cDbB94e302983578486A0A5f1B",
        "markets": {
            "usdc": {"comet": "0x8D38A3d6B3c3B7d96D6536DA7Eef94A9d7dbC991"},
            "weth": {"comet": "0x60F2058379716A64a7A5d29219397e79bC552194"},
        },
    },
    2020: {
        "chain_name": "ronin",
        "configurator": "0x966c72F456FC248D458784EF3E0b6d042be115F2",
        "rewards": "0x31CdEe8609Bc15fD33cc525f101B70a81b2B1E59",
        "bulker": "0x840281FaD56DD88afba052B7F18Be2A65796Ecc6",
        "markets": {
            "weth": {"comet": "0x4006eD4097Ee51c09A04c3B0951D28CCf19e6DFE"},
            "wron": {"comet": "0xc0Afdbd1cEB621Ef576BA969ce9D4ceF78Dbc0c0"},
        },
    },
    130: {
        "chain_name": "unichain",
        "configurator": "0x8df378453Ff9dEFFa513367CDF9b3B53726303e9",
        "rewards": "0x6f7D514bbD4aFf3BcD1140B7344b32f063dEe486",
        "bulker": "0x58EbB8Db8b4FdF2dCbbB16E04c2F5b952963B514",
        "markets": {
            "usdc": {"comet": "0x2c7118c4C88B9841FCF839074c26Ae8f035f2921"},
            "weth": {"comet": "0x6C987dDE50dB1dcDd32Cd4175778C2a291978E2a"},
        },
    },
    5000: {
        "chain_name": "mantle",
        "configurator": "0xb77Cd4cD000957283D8BAf53cD782ECf029cF7DB",
        "rewards": "0xCd83CbBFCE149d141A5171C3D6a0F0fCCeE225Ab",
        "bulker": "0x67DFCa85CcEEFA2C5B1dB4DEe3BEa716A28B9baa",
        "markets": {
            "usde": {"comet": "0x606174f62cd968d8e684c645080fa694c1D7786E"},
        },
    },
}
