from __future__ import annotations

from eth_utils import to_checksum_address

# SparkLend per-chain deployments.
#
# Sources:
# - Spark docs deployments page
# - Spark Address Registry (Pool, DataProvider, Incentives, WETH Gateway, Oracle)
#
# Note: Addresses are expected to be checksum addresses.
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
        "acl_manager": to_checksum_address(
            "0xdA135Cd78A086025BcdC87B038a1C462032b510C"
        ),
        "emission_manager": to_checksum_address(
            "0xf09e48dd4CA8e76F63a57ADd428bB06fee7932a4"
        ),
        "treasury": to_checksum_address(
            "0xb137E7d16564c81ae2b0C8ee6B55De81dd46ECe5"
        ),
        "treasury_controller": to_checksum_address(
            "0x92eF091C5a1E01b3CE1ba0D0150C84412d818F7a"
        ),
        "dai_treasury": to_checksum_address(
            "0x856900aa78e856a5df1a2665eE3a66b2487cD68f"
        ),
    },
    # Gnosis Chain
    100: {
        "chain_name": "gnosis",
        "pool_addresses_provider_registry": to_checksum_address(
            "0x49d24798d3b84965F0d1fc8684EF6565115e70c1"
        ),
        "pool_addresses_provider": to_checksum_address(
            "0xA98DaCB3fC964A6A0d2ce3B77294241585EAbA6d"
        ),
        "pool": to_checksum_address("0x2Dae5307c5E3FD1CF5A72Cb6F698f915860607e0"),
        "pool_configurator": to_checksum_address(
            "0x2Fc8823E1b967D474b47Ae0aD041c2ED562ab588"
        ),
        "protocol_data_provider": to_checksum_address(
            "0x2a002054A06546bB5a264D57A81347e23Af91D18"
        ),
        "ui_pool_data_provider": to_checksum_address(
            "0xF028c2F4b19898718fD0F77b9b881CbfdAa5e8Bb"
        ),
        "ui_incentive_data_provider": to_checksum_address(
            "0xA7F8A757C4f7696c015B595F51B2901AC0121B18"
        ),
        "rewards_controller": to_checksum_address(
            "0x98e6BcBA7d5daFbfa4a92dAF08d3d7512820c30C"
        ),
        "oracle": to_checksum_address("0x8105f69D9C41644c6A0803fDA7D03Aa70996cFD9"),
        "wrapped_native_gateway": to_checksum_address(
            "0xBD7D6a9ad7865463DE44B05F04559f65e3B11704"
        ),
        "acl_manager": to_checksum_address(
            "0x86C71796CcDB31c3997F8Ec5C2E3dB3e9e40b985"
        ),
        "emission_manager": to_checksum_address(
            "0x4d988568b5f0462B08d1F40bA1F5f17ad2D24F76"
        ),
        "treasury": to_checksum_address(
            "0xb9E6DBFa4De19CCed908BcbFe1d015190678AB5f"
        ),
        "treasury_controller": to_checksum_address(
            "0x8220096398c3Dc2644026E8864f5D80Ef613B437"
        ),
    },
}


# Convenience reserve maps from Spark registry pages. Prefer runtime discovery
# in production code (AaveProtocolDataProvider.getAllReservesTokens + friends).
SPARKLEND_RESERVES_ETHEREUM: dict[str, dict[str, str]] = {
    "CBBTC": {
        "underlying": to_checksum_address("0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"),
        "supply_token": to_checksum_address("0xb3973D459df38ae57797811F2A1fd061DA1BC123"),
        "variable_debt_token": to_checksum_address(
            "0x661fE667D2103eb52d3632a3eB2cAbd123F27938"
        ),
    },
    "DAI": {
        "underlying": to_checksum_address("0x6B175474E89094C44Da98b954EedeAC495271d0F"),
        "supply_token": to_checksum_address("0x4DEDf26112B3Ec8eC46e7E31EA5e123490B05B8B"),
        "variable_debt_token": to_checksum_address(
            "0xf705d2B7e92B3F38e6ae7afaDAA2fEE110fE5914"
        ),
    },
    "EZETH": {
        "underlying": to_checksum_address("0xbf5495Efe5DB9ce00f80364C8B423567e58d2110"),
        "supply_token": to_checksum_address("0xB131cD463d83782d4DE33e00e35EF034F0869bA1"),
        "variable_debt_token": to_checksum_address(
            "0xB0B14Dd477E6159B4F3F210cF45F0954F57c0FAb"
        ),
    },
    "GNO": {
        "underlying": to_checksum_address("0x6810e776880C02933D47DB1b9fc05908e5386b96"),
        "supply_token": to_checksum_address("0x7b481aCC9fDADDc9af2cBEA1Ff2342CB1733E50F"),
        "variable_debt_token": to_checksum_address(
            "0x57a2957651DA467fCD4104D749f2F3684784c25a"
        ),
    },
    "LBTC": {
        "underlying": to_checksum_address("0x8236a87084f8B84306f72007F36F2618A5634494"),
        "supply_token": to_checksum_address("0xa9d4EcEBd48C282a70CfD3c469d6C8F178a5738E"),
        "variable_debt_token": to_checksum_address(
            "0x096bdDFEE63F44A97cC6D2945539Ee7C8f94637D"
        ),
    },
    "PYUSD": {
        "underlying": to_checksum_address("0x6c3ea9036406852006290770BEdFcAbA0e23A0e8"),
        "supply_token": to_checksum_address("0x779224df1c756b4EDD899854F32a53E8c2B2ce5d"),
        "variable_debt_token": to_checksum_address(
            "0x3357D2DB7763D6Cd3a99f0763EbF87e0096D95f9"
        ),
    },
    "RETH": {
        "underlying": to_checksum_address("0xae78736Cd615f374D3085123A210448E74Fc6393"),
        "supply_token": to_checksum_address("0x9985dF20D7e9103ECBCeb16a84956434B6f06ae8"),
        "variable_debt_token": to_checksum_address(
            "0xBa2C8F2eA5B56690bFb8b709438F049e5Dd76B96"
        ),
    },
    "RSETH": {
        "underlying": to_checksum_address("0xA1290d69c65A6Fe4DF752f95823fae25cB99e5A7"),
        "supply_token": to_checksum_address("0x856f1Ea78361140834FDCd0dB0b08079e4A45062"),
        "variable_debt_token": to_checksum_address(
            "0xc528F0C91CFAE4fd86A68F6Dfd4d7284707Bec68"
        ),
    },
    "SDAI": {
        "underlying": to_checksum_address("0x83F20F44975D03b1b09e64809B757c47f942BEeA"),
        "supply_token": to_checksum_address("0x78f897F0fE2d3B5690EbAe7f19862DEacedF10a7"),
        "variable_debt_token": to_checksum_address(
            "0xaBc57081C04D921388240393ec4088Aa47c6832B"
        ),
    },
    "SUSDS": {
        "underlying": to_checksum_address("0xa3931d71877C0E7a3148CB7Eb4463524FEc27fbD"),
        "supply_token": to_checksum_address("0x6715bc100A183cc65502F05845b589c1919ca3d3"),
        "variable_debt_token": to_checksum_address(
            "0x4e89b83f426fED3f2EF7Bb2d7eb5b53e288e1A13"
        ),
    },
    "TBTC": {
        "underlying": to_checksum_address("0x18084fbA666a33d37592fA2633fD49a74DD93a88"),
        "supply_token": to_checksum_address("0xce6Ca9cDce00a2b0c0d1dAC93894f4Bd2c960567"),
        "variable_debt_token": to_checksum_address(
            "0x764591dC9ba21c1B92049331b80b6E2a2acF8B17"
        ),
    },
    "USDC": {
        "underlying": to_checksum_address("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"),
        "supply_token": to_checksum_address("0x377C3bd93f2a2984E1E7bE6A5C22c525eD4A4815"),
        "variable_debt_token": to_checksum_address(
            "0x7B70D04099CB9cfb1Db7B6820baDAfB4C5C70A67"
        ),
    },
    "USDS": {
        "underlying": to_checksum_address("0xdC035D45d973E3EC169d2276DDab16f1e407384F"),
        "supply_token": to_checksum_address("0xC02aB1A5eaA8d1B114EF786D9bde108cD4364359"),
        "variable_debt_token": to_checksum_address(
            "0x8c147debea24Fb98ade8dDa4bf142992928b449e"
        ),
    },
    "USDT": {
        "underlying": to_checksum_address("0xdAC17F958D2ee523a2206206994597C13D831ec7"),
        "supply_token": to_checksum_address("0xe7dF13b8e3d6740fe17CBE928C7334243d86c92f"),
        "variable_debt_token": to_checksum_address(
            "0x529b6158d1D2992E3129F7C69E81a7c677dc3B12"
        ),
    },
    "WBTC": {
        "underlying": to_checksum_address("0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599"),
        "supply_token": to_checksum_address("0x4197ba364AE6698015AE5c1468f54087602715b2"),
        "variable_debt_token": to_checksum_address(
            "0xf6fEe3A8aC8040C3d6d81d9A4a168516Ec9B51D2"
        ),
    },
    "WEETH": {
        "underlying": to_checksum_address("0xCd5fE23C85820F7B72D0926FC9b05b43E359b7ee"),
        "supply_token": to_checksum_address("0x3CFd5C0D4acAA8Faee335842e4f31159fc76B008"),
        "variable_debt_token": to_checksum_address(
            "0xc2bD6d2fEe70A0A73a33795BdbeE0368AeF5c766"
        ),
    },
    "WETH": {
        "underlying": to_checksum_address("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"),
        "supply_token": to_checksum_address("0x59cD1C87501baa753d0B5B5Ab5D8416A45cD71DB"),
        "variable_debt_token": to_checksum_address(
            "0x2e7576042566f8D6990e07A1B61Ad1efd86Ae70d"
        ),
    },
    "WSTETH": {
        "underlying": to_checksum_address("0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0"),
        "supply_token": to_checksum_address("0x12B54025C112Aa61fAce2CDB7118740875A566E9"),
        "variable_debt_token": to_checksum_address(
            "0xd5c3E3B566a42A6110513Ac7670C1a86D76E13E6"
        ),
    },
}


SPARKLEND_RESERVES_GNOSIS: dict[str, dict[str, str]] = {
    "GNO": {
        "underlying": to_checksum_address("0x9C58BAcC331c9aa871AFD802DB6379a98e80CEdb"),
        "supply_token": to_checksum_address("0x5671b0B8aC13DC7813D36B99C21c53F6cd376a14"),
        "stable_debt_token": to_checksum_address(
            "0x2f589BADbE2024a94f144ef24344aF91dE21a33c"
        ),
        "variable_debt_token": to_checksum_address(
            "0xd4bAbF714964E399f95A7bb94B3DeaF22d9F575d"
        ),
    },
    "WETH": {
        "underlying": to_checksum_address("0x6A023CCd1ff6F2045C3309768eAd9E68F978f6e1"),
        "supply_token": to_checksum_address("0x629D562E92fED431122e865Cc650Bc6bdE6B96b0"),
        "stable_debt_token": to_checksum_address(
            "0xe21Bf3FB5A2b5Bf7BAE8c6F1696c4B097F5D2f93"
        ),
        "variable_debt_token": to_checksum_address(
            "0x0aD6cCf9a2e81d4d48aB7db791e9da492967eb84"
        ),
    },
    "WSTETH": {
        "underlying": to_checksum_address("0x6C76971f98945AE98dD7d4DFcA8711ebea946eA6"),
        "supply_token": to_checksum_address("0x9Ee4271E17E3a427678344fd2eE64663Cb78B4be"),
        "stable_debt_token": to_checksum_address(
            "0x0F0e336Ab69D9516A9acF448bC59eA0CE79E4a42"
        ),
        "variable_debt_token": to_checksum_address(
            "0x3294dA2E28b29D1c08D556e2B86879d221256d31"
        ),
    },
    "WXDAI": {
        "underlying": to_checksum_address("0xe91D153E0b41518A2Ce8Dd3D7944Fa863463a97d"),
        "supply_token": to_checksum_address("0xC9Fe2D32E96Bb364c7d29f3663ed3b27E30767bB"),
        "stable_debt_token": to_checksum_address(
            "0xab1B62A1346Acf534b581684940E2FD781F2EA22"
        ),
        "variable_debt_token": to_checksum_address(
            "0x868ADfDf12A86422524EaB6978beAE08A0008F37"
        ),
    },
    "SXDAI": {
        "underlying": to_checksum_address("0xaf204776c7245bF4147c2612BF6e5972Ee483701"),
        "supply_token": to_checksum_address("0xE877b96caf9f180916bF2B5Ce7Ea8069e0123182"),
        "stable_debt_token": to_checksum_address(
            "0x2cF710377b3576287Be7cf352FF75D4472902789"
        ),
        "variable_debt_token": to_checksum_address(
            "0x1022E390E2457A78E18AEEE0bBf0E96E482EeE19"
        ),
    },
    "USDC": {
        "underlying": to_checksum_address("0xDDAfbb505ad214D7b80b1f830fcCc89B60fb7A83"),
        "supply_token": to_checksum_address("0x5850D127a04ed0B4F1FCDFb051b3409FB9Fe6B90"),
        "stable_debt_token": to_checksum_address(
            "0x40BF0Bf6AECeE50eCE10C74E81a52C654A467ae4"
        ),
        "variable_debt_token": to_checksum_address(
            "0xBC4f20DAf4E05c17E93676D2CeC39769506b8219"
        ),
    },
    "USDCE": {
        "underlying": to_checksum_address("0x2a22f9c3b484c3629090FeED35F17Ff8F88f76F0"),
        "supply_token": to_checksum_address("0xA34DB0ee8F84C4B90ed268dF5aBbe7Dcd3c277ec"),
        "stable_debt_token": to_checksum_address(
            "0xC5dfde524371F9424c81F453260B2CCd24936c15"
        ),
        "variable_debt_token": to_checksum_address(
            "0x397b97b572281d0b3e3513BD4A7B38050a75962b"
        ),
    },
    "USDT": {
        "underlying": to_checksum_address("0x4ECaBa5870353805a9F068101A40E0f32ed605C6"),
        "supply_token": to_checksum_address("0x08B0cAebE352c3613302774Cd9B82D08afd7bDC4"),
        "stable_debt_token": to_checksum_address(
            "0x4cB3F681B5e393947BD1e5cAE84764f5892923C2"
        ),
        "variable_debt_token": to_checksum_address(
            "0x3A98aBC6F46CA2Fc6c7d06eD02184D63C55e19B2"
        ),
    },
    "EURE": {
        "underlying": to_checksum_address("0xcB444e90D8198415266c6a2724b7900fb12FC56E"),
        "supply_token": to_checksum_address("0x6dc304337BF3EB397241d1889cAE7da638e6e782"),
        "stable_debt_token": to_checksum_address(
            "0x80F87B8F9c1199e468923D8EE87cEE311690FDA6"
        ),
        "variable_debt_token": to_checksum_address(
            "0x0b33480d3FbD1E2dBE88c82aAbe191D7473759D5"
        ),
    },
}

