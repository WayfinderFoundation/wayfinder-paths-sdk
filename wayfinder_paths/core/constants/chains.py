CHAIN_ID_ETHEREUM = 1
CHAIN_ID_BASE = 8453
CHAIN_ID_ARBITRUM = 42161
CHAIN_ID_BSC = 56
CHAIN_ID_POLYGON = 137
CHAIN_ID_AVALANCHE = 43114
CHAIN_ID_HYPEREVM = 999

CHAIN_CODE_TO_ID = {
    "base": CHAIN_ID_BASE,
    "arbitrum": CHAIN_ID_ARBITRUM,
    "arbitrum-one": CHAIN_ID_ARBITRUM,
    "ethereum": CHAIN_ID_ETHEREUM,
    "mainnet": CHAIN_ID_ETHEREUM,
    "hyperevm": CHAIN_ID_HYPEREVM,
}

SUPPORTED_CHAINS = [
    CHAIN_ID_ETHEREUM,
    CHAIN_ID_BASE,
    CHAIN_ID_BSC,
    CHAIN_ID_ARBITRUM,
    CHAIN_ID_POLYGON,
    CHAIN_ID_HYPEREVM,
]

POA_MIDDLEWARE_CHAIN_IDS: set[int] = {
    CHAIN_ID_BSC,
    CHAIN_ID_POLYGON,
    CHAIN_ID_AVALANCHE,
}

PRE_EIP_1559_CHAIN_IDS: set[int] = {
    CHAIN_ID_BSC,
    CHAIN_ID_ARBITRUM,
}

CHAIN_EXPLORER_URLS: dict[int, str] = {
    CHAIN_ID_ETHEREUM: "https://etherscan.io/",
    CHAIN_ID_ARBITRUM: "https://arbiscan.io/",
    CHAIN_ID_BASE: "https://basescan.org/",
    CHAIN_ID_BSC: "https://bscscan.com/",
    CHAIN_ID_HYPEREVM: "https://hyperevmscan.io/",
}
