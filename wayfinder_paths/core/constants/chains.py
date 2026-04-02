CHAIN_ID_ETHEREUM = 1
CHAIN_ID_BASE = 8453
CHAIN_ID_ARBITRUM = 42161
CHAIN_ID_BSC = 56
CHAIN_ID_POLYGON = 137
CHAIN_ID_AVALANCHE = 43114
CHAIN_ID_PLASMA = 9745
CHAIN_ID_HYPEREVM = 999
CHAIN_ID_UNICHAIN = 130
CHAIN_ID_TEMPO = 4217
CHAIN_ID_MANTLE = 5000
CHAIN_ID_KATANA = 747474
CHAIN_ID_MONAD = 143
CHAIN_ID_MEGAETH = 4326

CHAIN_CODE_TO_ID = {
    "base": CHAIN_ID_BASE,
    "arbitrum": CHAIN_ID_ARBITRUM,
    "arbitrum-one": CHAIN_ID_ARBITRUM,
    "bsc": CHAIN_ID_BSC,
    "ethereum": CHAIN_ID_ETHEREUM,
    "mainnet": CHAIN_ID_ETHEREUM,
    "polygon": CHAIN_ID_POLYGON,
    "avalanche": CHAIN_ID_AVALANCHE,
    "plasma": CHAIN_ID_PLASMA,
    "hyperevm": CHAIN_ID_HYPEREVM,
    "unichain": CHAIN_ID_UNICHAIN,
    "tempo": CHAIN_ID_TEMPO,
    "mantle": CHAIN_ID_MANTLE,
    "katana": CHAIN_ID_KATANA,
    "monad": CHAIN_ID_MONAD,
    "megaeth": CHAIN_ID_MEGAETH,
}

CHAIN_ID_TO_CODE: dict[int, str] = {
    v: k for k, v in CHAIN_CODE_TO_ID.items() if k not in ("arbitrum-one", "mainnet")
}

SUPPORTED_CHAINS = [
    CHAIN_ID_ETHEREUM,
    CHAIN_ID_BASE,
    CHAIN_ID_BSC,
    CHAIN_ID_ARBITRUM,
    CHAIN_ID_POLYGON,
    CHAIN_ID_AVALANCHE,
    CHAIN_ID_PLASMA,
    CHAIN_ID_HYPEREVM,
    CHAIN_ID_UNICHAIN,
    CHAIN_ID_TEMPO,
    CHAIN_ID_MANTLE,
    CHAIN_ID_KATANA,
    CHAIN_ID_MONAD,
    CHAIN_ID_MEGAETH,
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
    CHAIN_ID_AVALANCHE: "https://snowtrace.io/",
    CHAIN_ID_PLASMA: "https://plasmascan.to/",
    CHAIN_ID_HYPEREVM: "https://hyperevmscan.io/",
    CHAIN_ID_UNICHAIN: "https://uniscan.xyz/",
    CHAIN_ID_TEMPO: "https://tempo.explorer.caldera.xyz/",
    CHAIN_ID_MANTLE: "https://mantlescan.xyz/",
    CHAIN_ID_KATANA: "https://katanascan.com/",
    CHAIN_ID_MONAD: "https://monadscan.com/",
    CHAIN_ID_MEGAETH: "https://mega.etherscan.io/",
}

ETHERSCAN_V2_API_URL = "https://api.etherscan.io/v2/api"
