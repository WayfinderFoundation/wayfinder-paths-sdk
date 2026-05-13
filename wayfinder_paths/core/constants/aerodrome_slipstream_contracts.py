from __future__ import annotations

from wayfinder_paths.core.constants.aerodrome_contracts import AERODROME_BY_CHAIN
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE
from wayfinder_paths.core.constants.contracts import BASE_WETH

AERODROME_SLIPSTREAM_DEPLOYMENT_INITIAL = "initial"
AERODROME_SLIPSTREAM_DEPLOYMENT_GAUGE_CAPS = "gauge_caps"

AERODROME_SLIPSTREAM_BY_CHAIN: dict[int, dict[str, object]] = {
    CHAIN_ID_BASE: {
        "chain_name": "base",
        "aero": AERODROME_BY_CHAIN[CHAIN_ID_BASE]["aero"],
        "voter": AERODROME_BY_CHAIN[CHAIN_ID_BASE]["voter"],
        "voting_escrow": AERODROME_BY_CHAIN[CHAIN_ID_BASE]["voting_escrow"],
        "rewards_distributor": AERODROME_BY_CHAIN[CHAIN_ID_BASE]["rewards_distributor"],
        "weth": BASE_WETH,
        "deployments": {
            "initial": {
                "gauge_factory": "0xD30677bd8dd15132F251Cb54CbDA552d2A05Fb08",
                "gauge_implementation": "0xF5601F95708256A118EF5971820327F362442D2d",
                "mixed_quoter": "0x0A5aA5D3a4d28014f967Bf0f29EAA3FF9807D5c6",
                "nonfungible_position_manager": "0x827922686190790b37229fd06084350E74485b72",
                "nonfungible_token_position_descriptor": "0x01b0CaCB9A8004e08D075c919B5dF3b59FD53c55",
                "pool_factory": "0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A",
                "pool_implementation": "0xeC8E5342B19977B4eF8892e02D8DAEcfa1315831",
                "quoter_v2": "0x254cF9E1E6e233aa1AC962CB9B05b2cfeAaE15b0",
                "custom_swap_fee_module": "0xF4171B0953b52Fa55462E4d76ecA1845Db69af00",
                "custom_unstaked_fee_module": "0x0AD08370c76Ff426F534bb2AFFD9b5555338ee68",
                "swap_router": "0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5",
                "sugar_helper": "0x0AD09A66af0154a84e86F761313d02d0abB6edd5",
                "dynamic_swap_fee_module": "0xDB45818A6db280ecfeB33cbeBd445423d0216b5D",
            },
            "gauge_caps": {
                "gauge_factory": "0xB630227a79707D517320b6c0f885806389dFcbB3",
                "gauge_implementation": "0xC0d2086B6f70C0C40423626167096c6196cFA0c8",
                "mixed_quoter": "0x49540630A4d2CE67d54450D007D634F4c45B4f4f",
                "nonfungible_position_manager": "0xa990C6a764b73BF43cee5Bb40339c3322FB9D55F",
                "nonfungible_token_position_descriptor": "0xf632031B94D72deE0D99DeF846c9b6211041337f",
                "pool_factory": "0xaDe65c38CD4849aDBA595a4323a8C7DdfE89716a",
                "pool_implementation": "0x942e97a4c6FdC38B4CD1c0298D37d81fDD8E5A16",
                "quoter": "0x3d4C22254F86f64B7eC90ab8F7aeC1FBFD271c6C",
                "swap_fee_module": "0x5264Eeeab16037A7A7AF15Ff69A470af6e2a2223",
                "swap_router": "0xcbBb8035cAc7D4B3Ca7aBb74cF7BdF900215Ce0D",
                "unstaked_fee_module": "0xCCC21f4750E8B3E9C095BCB5d2fF59247A2CCD35",
                "dynamic_swap_fee_module": "0x00cB12a1c84dfC1b9c70734C0385E769Bc86e9Ef",
                "redistributor": "0x11a53f31Bf406de59fCf9613E1922bd3E283A4B4",
            },
        },
    }
}
