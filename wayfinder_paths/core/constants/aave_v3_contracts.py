from __future__ import annotations

# Aave v3 per-chain deployments.
#
# Notes:
# - HyperEVM is supported via Hyperlend (Aave-style fork), not Aave itself.
# - Addresses are stored lowercase; checksum at point of use.
AAVE_V3_BY_CHAIN: dict[int, dict[str, str]] = {
    # Ethereum
    1: {
        "pool": "0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2",
        "pool_addresses_provider": "0x2f39d218133afab8f2b819b1066c7e434ad94e9e",
        "ui_pool_data_provider": "0x56b7a1012765c285afac8b8f25c69bf10ccfe978",
        "ui_incentive_data_provider": "0xe3dff4052f0bf6134acb73beae8fe2317d71f047",
        "rewards_controller": "0x8164cc65827dcfe994ab23944cbc90e0aa80bfcb",
        "wrapped_token_gateway": "0xd01607c3c5ecaba394d8be377a08590149325722",
        "oracle": "0x54586be62e3c3580375ae3723c145253060ca0c2",
    },
    # Polygon PoS
    137: {
        "pool": "0x794a61358d6845594f94dc1db02a252b5b4814ad",
        "pool_addresses_provider": "0xa97684ead0e402dc232d5a977953df7ecbab3cdb",
        "ui_pool_data_provider": "0xfa1a7c4a8a63c9cab150529c26f182cbb5500944",
        "ui_incentive_data_provider": "0x91e04cf78e53aebe609e8a7f2003e7eecd743f2b",
        "rewards_controller": "0x929ec64c34a17401f460460d4b9390518e5b473e",
        "wrapped_token_gateway": "0xbc302053db3aa514a3c86b9221082f162b91ad63",
        "oracle": "0xb023e699f5a33916ea823a16485e259257ca8bd1",
    },
    # Base
    8453: {
        "pool": "0xa238dd80c259a72e81d7e4664a9801593f98d1c5",
        "pool_addresses_provider": "0xe20fcbdbffc4dd138ce8b2e6fbb6cb49777ad64d",
        "ui_pool_data_provider": "0xb84a20e848bae3e13897934bb4e74e2225f4546b",
        "ui_incentive_data_provider": "0x91e04cf78e53aebe609e8a7f2003e7eecd743f2b",
        "rewards_controller": "0xf9cc4f0d883f1a1eb2c253bdb46c254ca51e1f44",
        "wrapped_token_gateway": "0xa0d9c1e9e48ca30c8d8c3b5d69ff5dc1f6dffc24",
        "oracle": "0x2cc0fc26ed4563a5ce5e8bdcfe1a2878676ae156",
    },
    # BNB Smart Chain
    56: {
        "pool": "0x6807dc923806fe8fd134338eabca509979a7e0cb",
        "pool_addresses_provider": "0xff75b6da14ffbbfd355daf7a2731456b3562ba6d",
        "ui_pool_data_provider": "0x632b5dfc315b228bfe779e6442322ad8a110ea13",
        "ui_incentive_data_provider": "0x5c5228ac8bc1528482514af3e27e692495148717",
        "rewards_controller": "0xc206c2764a9dbf27d599613b8f9a63acd1160ab4",
        "wrapped_token_gateway": "0x0c2c95b24529664fe55d4437d7a31175cfe6c4f7",
        "oracle": "0x39bc1bfda2130d6bb6dbefd366939b4c7aa7c697",
    },
    # Arbitrum One
    42161: {
        "pool": "0x794a61358d6845594f94dc1db02a252b5b4814ad",
        "pool_addresses_provider": "0xa97684ead0e402dc232d5a977953df7ecbab3cdb",
        "ui_pool_data_provider": "0x13c833256bd767da2320d727a3691baff3770e39",
        "ui_incentive_data_provider": "0x68100bd5345ea474d93577127c11f39ff8463e93",
        "rewards_controller": "0x929ec64c34a17401f460460d4b9390518e5b473e",
        "wrapped_token_gateway": "0x5283beced7adf6d003225c13896e536f2d4264ff",
        "oracle": "0xb56c2f0b653b2e0b10c9b928c8580ac5df02c7c7",
    },
    # Avalanche C-Chain
    43114: {
        "pool": "0x794a61358d6845594f94dc1db02a252b5b4814ad",
        "pool_addresses_provider": "0xa97684ead0e402dc232d5a977953df7ecbab3cdb",
        "ui_pool_data_provider": "0x3518e8927a7827cddaf841872453003ca95906a3",
        "ui_incentive_data_provider": "0x99732d5da21f44f9e45e36ef9da4b1df2eb0b28e",
        "rewards_controller": "0x929ec64c34a17401f460460d4b9390518e5b473e",
        "wrapped_token_gateway": "0x2825ce5921538d17cc15ae00a8b24ff759c6cdae",
        "oracle": "0xebd36016b3ed09d4693ed4251c67bd858c3c7c9c",
    },
    # Plasma
    9745: {
        "pool": "0x925a2a7214ed92428b5b1b090f80b25700095e12",
        "pool_addresses_provider": "0x061d8e131f26512348ee5fa42e2df1ba9d6505e9",
        "ui_pool_data_provider": "0xda549478fd5c2bdb9e5eb000d0ff2554771598c7",
        "ui_incentive_data_provider": "0xcb85c501b3a5e9851850d66648d69b26a4c90942",
        "rewards_controller": "0x3a57eaa3ca3794d66977326af7991eb3f6dd5a5a",
        "wrapped_token_gateway": "0x54bdcc37c4143f944a3ee51c892a6cbdf305e7a0",
        "oracle": "0x33e0b3fc976dc9c516926ba48cfc0a9e10a2aaa5",
    },
}
