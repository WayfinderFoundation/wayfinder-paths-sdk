from __future__ import annotations

from eth_utils import to_checksum_address

# Aave v3 per-chain deployments.
#
# Notes:
# - HyperEVM is supported via Hyperlend (Aave-style fork), not Aave itself.
# - Addresses are expected to be checksum addresses.
AAVE_V3_BY_CHAIN: dict[int, dict[str, str]] = {
    # Ethereum
    1: {
        "pool": to_checksum_address("0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2"),
        "pool_addresses_provider": to_checksum_address(
            "0x2f39d218133AFaB8F2B819B1066c7E434Ad94E9e"
        ),
        "ui_pool_data_provider": to_checksum_address(
            "0x56b7A1012765C285afAC8b8F25C69Bf10ccfE978"
        ),
        "ui_incentive_data_provider": to_checksum_address(
            "0xe3dFf4052F0bF6134ACb73bEaE8fe2317d71F047"
        ),
        "rewards_controller": to_checksum_address(
            "0x8164Cc65827dcFe994AB23944CBC90e0aa80bFcb"
        ),
        "wrapped_token_gateway": to_checksum_address(
            "0xd01607c3C5eCABa394D8be377a08590149325722"
        ),
        "oracle": to_checksum_address("0x54586bE62E3c3580375aE3723C145253060Ca0C2"),
    },
    # Polygon PoS
    137: {
        "pool": to_checksum_address("0x794a61358D6845594F94dc1DB02A252b5b4814aD"),
        "pool_addresses_provider": to_checksum_address(
            "0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb"
        ),
        "ui_pool_data_provider": to_checksum_address(
            "0xFa1A7c4a8A63C9CAb150529c26f182cBB5500944"
        ),
        "ui_incentive_data_provider": to_checksum_address(
            "0x91E04cf78e53aEBe609e8a7f2003e7EECD743F2B"
        ),
        "rewards_controller": to_checksum_address(
            "0x929EC64c34a17401F460460D4B9390518E5B473e"
        ),
        "wrapped_token_gateway": to_checksum_address(
            "0xBC302053db3aA514A3c86B9221082f162B91ad63"
        ),
        "oracle": to_checksum_address("0xb023e699F5a33916Ea823A16485e259257cA8Bd1"),
    },
    # Base
    8453: {
        "pool": to_checksum_address("0xA238Dd80C259a72e81d7e4664a9801593F98d1c5"),
        "pool_addresses_provider": to_checksum_address(
            "0xe20fCBdBfFC4Dd138cE8b2E6FBb6CB49777ad64D"
        ),
        "ui_pool_data_provider": to_checksum_address(
            "0xb84A20e848baE3e13897934bB4e74E2225f4546B"
        ),
        "ui_incentive_data_provider": to_checksum_address(
            "0x91E04cf78e53aEBe609e8a7f2003e7EECD743F2B"
        ),
        "rewards_controller": to_checksum_address(
            "0xf9cc4F0D883F1a1eb2c253bdb46c254Ca51E1F44"
        ),
        "wrapped_token_gateway": to_checksum_address(
            "0xa0d9C1E9E48Ca30c8d8C3B5D69FF5dc1f6DFfC24"
        ),
        "oracle": to_checksum_address("0x2Cc0Fc26eD4563A5ce5e8bdcfe1A2878676Ae156"),
    },
    # BNB Smart Chain
    56: {
        "pool": to_checksum_address("0x6807dc923806fE8Fd134338EABCA509979a7e0cB"),
        "pool_addresses_provider": to_checksum_address(
            "0xff75B6da14FfbbfD355Daf7a2731456b3562Ba6D"
        ),
        "ui_pool_data_provider": to_checksum_address(
            "0x632b5Dfc315b228bfE779E6442322Ad8a110Ea13"
        ),
        "ui_incentive_data_provider": to_checksum_address(
            "0x5c5228aC8BC1528482514aF3e27E692495148717"
        ),
        "rewards_controller": to_checksum_address(
            "0xC206C2764A9dBF27d599613b8F9A63ACd1160ab4"
        ),
        "wrapped_token_gateway": to_checksum_address(
            "0x0c2C95b24529664fE55D4437D7A31175CFE6c4f7"
        ),
        "oracle": to_checksum_address("0x39bc1bfDa2130d6Bb6DBEfd366939b4c7aa7C697"),
    },
    # Arbitrum One
    42161: {
        "pool": to_checksum_address("0x794a61358D6845594F94dc1DB02A252b5b4814aD"),
        "pool_addresses_provider": to_checksum_address(
            "0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb"
        ),
        "ui_pool_data_provider": to_checksum_address(
            "0x13c833256BD767da2320d727a3691BAff3770E39"
        ),
        "ui_incentive_data_provider": to_checksum_address(
            "0x68100bD5345eA474D93577127C11F39FF8463e93"
        ),
        "rewards_controller": to_checksum_address(
            "0x929EC64c34a17401F460460D4B9390518E5B473e"
        ),
        "wrapped_token_gateway": to_checksum_address(
            "0x5283BEcEd7ADF6D003225C13896E536f2D4264FF"
        ),
        "oracle": to_checksum_address("0xb56c2F0B653B2e0b10C9b928C8580Ac5Df02C7C7"),
    },
    # Avalanche C-Chain
    43114: {
        "pool": to_checksum_address("0x794a61358D6845594F94dc1DB02A252b5b4814aD"),
        "pool_addresses_provider": to_checksum_address(
            "0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb"
        ),
        "ui_pool_data_provider": to_checksum_address(
            "0x3518E8927A7827CDdAf841872453003CA95906A3"
        ),
        "ui_incentive_data_provider": to_checksum_address(
            "0x99732D5dA21f44f9e45e36eF9da4B1df2Eb0b28E"
        ),
        "rewards_controller": to_checksum_address(
            "0x929EC64c34a17401F460460D4B9390518E5B473e"
        ),
        "wrapped_token_gateway": to_checksum_address(
            "0x2825cE5921538d17cc15Ae00a8B24fF759C6CDaE"
        ),
        "oracle": to_checksum_address("0xEBd36016B3eD09D4693Ed4251c67Bd858c3c7C9C"),
    },
    # Plasma
    9745: {
        "pool": to_checksum_address("0x925a2A7214Ed92428B5b1B090F80b25700095e12"),
        "pool_addresses_provider": to_checksum_address(
            "0x061D8e131F26512348ee5FA42e2DF1bA9d6505E9"
        ),
        "ui_pool_data_provider": to_checksum_address(
            "0xdA549478Fd5C2BdB9e5eB000D0ff2554771598C7"
        ),
        "ui_incentive_data_provider": to_checksum_address(
            "0xcb85C501B3A5e9851850d66648d69B26A4c90942"
        ),
        "rewards_controller": to_checksum_address(
            "0x3A57eAa3Ca3794D66977326af7991eB3F6dD5a5A"
        ),
        "wrapped_token_gateway": to_checksum_address(
            "0x54BDcc37c4143f944A3EE51C892a6cBDF305E7a0"
        ),
        "oracle": to_checksum_address("0x33E0b3fc976DC9C516926BA48CfC0A9E10a2aAA5"),
    },
}
