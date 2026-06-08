from __future__ import annotations

from typing import Any

from eth_utils import to_checksum_address

from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE

CHAIN_ID_OPTIMISM = 10
CHAIN_ID_MOONBEAM = 1284
CHAIN_ID_MOONRIVER = 1285

ZERO_ADDRESS = to_checksum_address("0x0000000000000000000000000000000000000000")

# Sources:
# - Moonwell contracts docs:
#   https://docs.moonwell.fi/moonwell/protocol-information/contracts
# - Moonwell SDK environment definitions:
#   https://github.com/moonwell-fi/moonwell-sdk/tree/main/src/environments/definitions
MOONWELL_BY_CHAIN: dict[int, dict[str, Any]] = {
    CHAIN_ID_BASE: {
        "network": "Base",
        "chain_name": "base",
        "comptroller": to_checksum_address(
            "0xfBb21d0380beE3312B33c4353c8936a0F13EF26C"
        ),
        "views": to_checksum_address("0x6834770aba6c2028f448e3259ddee4bcb879d459"),
        "sdk_views": to_checksum_address("0x821Ff3a967b39bcbE8A018a9b1563EAf878bad39"),
        "reward_distributor": to_checksum_address(
            "0xe9005b078701e2A0948D2EaC43010D35870Ad9d2"
        ),
        "governance_token": to_checksum_address(
            "0xA88594D404727625A9437C3f886C7643872296AE"
        ),
        "wrapped_native_token": to_checksum_address(
            "0x4200000000000000000000000000000000000006"
        ),
        "sample_mtoken": to_checksum_address(
            "0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22"
        ),
        "morpho": {
            "morpho_blue": to_checksum_address(
                "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
            ),
            "base_bundler": to_checksum_address(
                "0x6BFd8137e702540E7A42B74178A4a49Ba43920C4"
            ),
            "bundler": to_checksum_address(
                "0xb98c948CFA24072e58935BC004a8A7b376AE746A"
            ),
            "public_allocator": to_checksum_address(
                "0xA090dD1a701408Df1d4d0B85b716c87565f90467"
            ),
            "views": to_checksum_address("0xc72fCC9793a10b9c363EeaAcaAbe422E0672B42B"),
            "views_v2": to_checksum_address(
                "0x8D189997ccD6ab6909eF89836e5bcEf94599Cce0"
            ),
        },
    },
    CHAIN_ID_OPTIMISM: {
        "network": "OP Mainnet",
        "chain_name": "optimism",
        "comptroller": to_checksum_address(
            "0xCa889f40aae37FFf165BccF69aeF1E82b5C511B9"
        ),
        "views": to_checksum_address("0xD6C66868f937f00604d0FB860241970D6CC2CBfE"),
        "reward_distributor": to_checksum_address(
            "0xF9524bfa18C19C3E605FbfE8DFd05C6e967574Aa"
        ),
        "governance_token": to_checksum_address(
            "0xA88594D404727625A9437C3f886C7643872296AE"
        ),
        "wrapped_native_token": to_checksum_address(
            "0x4200000000000000000000000000000000000006"
        ),
        "sample_mtoken": to_checksum_address(
            "0x8E08617b0d66359D73Aa11E11017834C29155525"
        ),
        "morpho": {
            "morpho_blue": to_checksum_address(
                "0xce95AfbB8EA029495c66020883F87aaE8864AF92"
            ),
            "base_bundler": to_checksum_address(
                "0xFBCd3C258feB131D8E038F2A3a670A7bE0507C05"
            ),
            "bundler": to_checksum_address(
                "0x79481C87f24A3C4332442A2E9faaf675e5F141f0"
            ),
            "public_allocator": to_checksum_address(
                "0x0d68a97324E602E02799CD83B42D337207B40658"
            ),
            "views": to_checksum_address("0x90AA62DD4Fd10955A46f77176019d908849451F8"),
        },
    },
    CHAIN_ID_MOONBEAM: {
        "network": "Moonbeam",
        "chain_name": "moonbeam",
        "comptroller": to_checksum_address(
            "0x8E00D5e02E65A19337Cdba98bbA9F84d4186a180"
        ),
        "views": to_checksum_address("0xe76C8B8706faC85a8Fbdcac3C42e3E7823c73994"),
        "reward_distributor": None,
        "governance_token": to_checksum_address(
            "0x511aB53F793683763E5a8829738301368a2411E3"
        ),
        "wrapped_native_token": to_checksum_address(
            "0xAcc15dC74880C9944775448304B263D191c6077F"
        ),
        "sample_mtoken": to_checksum_address(
            "0x22b1a40e3178fe7C7109eFCc247C5bB2B34ABe32"
        ),
    },
    CHAIN_ID_MOONRIVER: {
        "network": "Moonriver",
        "chain_name": "moonriver",
        "comptroller": to_checksum_address(
            "0x0b7a0EAA884849c6Af7a129e899536dDDcA4905E"
        ),
        "views": to_checksum_address("0x6F0cC02e5a7640B28F538fcc06bCA3BdFA57d1BB"),
        "reward_distributor": None,
        "governance_token": to_checksum_address(
            "0xBb8d88bcD9749636BC4D2bE22aaC4Bb3B01A58F1"
        ),
        "wrapped_native_token": to_checksum_address(
            "0x98878B06940aE243284CA214f92Bb71a2b032B8A"
        ),
        "sample_mtoken": to_checksum_address(
            "0xd0670AEe3698F66e2D4dAf071EB9c690d978BFA8"
        ),
    },
}


def _market(
    mtoken: str,
    underlying: str,
    *,
    symbol: str,
    underlying_symbol: str,
    deprecated: bool = False,
    bad_debt: bool = False,
    native: bool = False,
) -> dict[str, Any]:
    return {
        "mtoken": mtoken,
        "underlying": underlying,
        "symbol": symbol,
        "underlying_symbol": underlying_symbol,
        "deprecated": deprecated,
        "bad_debt": bad_debt,
        "native": native,
    }


MOONWELL_CORE_MARKETS_BY_CHAIN: dict[int, dict[str, dict[str, Any]]] = {
    CHAIN_ID_BASE: {
        "USDC": _market(
            to_checksum_address("0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22"),
            to_checksum_address("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"),
            symbol="mUSDC",
            underlying_symbol="USDC",
        ),
        "ETH": _market(
            to_checksum_address("0x628ff693426583D9a7FB391E54366292F509D457"),
            to_checksum_address("0x4200000000000000000000000000000000000006"),
            symbol="mWETH",
            underlying_symbol="WETH",
        ),
        "cbETH": _market(
            to_checksum_address("0x3bf93770f2d4a794c3d9EBEfBAeBAE2a8f09A5E5"),
            to_checksum_address("0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22"),
            symbol="mcbETH",
            underlying_symbol="cbETH",
        ),
        "wstETH": _market(
            to_checksum_address("0x627Fe393Bc6EdDA28e99AE648fD6fF362514304b"),
            to_checksum_address("0xc1cba3fcea344f92d9239c08c0568f6f2f0ee452"),
            symbol="mwstETH",
            underlying_symbol="wstETH",
        ),
        "rETH": _market(
            to_checksum_address("0xCB1DaCd30638ae38F2B94eA64F066045B7D45f44"),
            to_checksum_address("0xb6fe221fe9eef5aba221c348ba20a1bf5e73624c"),
            symbol="mrETH",
            underlying_symbol="rETH",
        ),
        "weETH": _market(
            to_checksum_address("0xb8051464C8c92209C92F3a4CD9C73746C4c3CFb3"),
            to_checksum_address("0x04c0599ae5a44757c0af6f9ec3b93da8976c150a"),
            symbol="mweETH",
            underlying_symbol="weETH",
        ),
        "cbBTC": _market(
            to_checksum_address("0xF877ACaFA28c19b96727966690b2f44d35aD5976"),
            to_checksum_address("0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"),
            symbol="mcbBTC",
            underlying_symbol="cbBTC",
        ),
        "AERO": _market(
            to_checksum_address("0x73902f619CEB9B31FD8EFecf435CbDf89E369Ba6"),
            to_checksum_address("0x940181a94A35A4569E4529A3CDfB74e38FD98631"),
            symbol="mAERO",
            underlying_symbol="AERO",
        ),
        "DAI": _market(
            to_checksum_address("0x73b06D8d18De422E269645eaCe15400DE7462417"),
            to_checksum_address("0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb"),
            symbol="mDAI",
            underlying_symbol="DAI",
        ),
        "USDbC": _market(
            to_checksum_address("0x703843C3379b52F9FF486c9f5892218d2a065cC8"),
            to_checksum_address("0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca"),
            symbol="mUSDC",
            underlying_symbol="USDbC",
            deprecated=True,
        ),
        "EURC": _market(
            to_checksum_address("0xb682c840B5F4FC58B20769E691A6fa1305A501a2"),
            to_checksum_address("0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42"),
            symbol="mEURC",
            underlying_symbol="EURC",
        ),
        "wrsETH": _market(
            to_checksum_address("0xfC41B49d064Ac646015b459C522820DB9472F4B5"),
            to_checksum_address("0xEDfa23602D0EC14714057867A78d01e94176BEA0"),
            symbol="mwrsETH",
            underlying_symbol="wrsETH",
        ),
        "WELL": _market(
            to_checksum_address("0xdC7810B47eAAb250De623F0eE07764afa5F71ED1"),
            to_checksum_address("0xA88594D404727625A9437C3f886C7643872296AE"),
            symbol="mWELL",
            underlying_symbol="WELL",
        ),
        "USDS": _market(
            to_checksum_address("0xb6419c6C2e60c4025D6D06eE4F913ce89425a357"),
            to_checksum_address("0x820C137fa70C8691f0e44Dc420a5e53c168921Dc"),
            symbol="mUSDS",
            underlying_symbol="USDS",
        ),
        "tBTC": _market(
            to_checksum_address("0x9A858ebfF1bEb0D3495BB0e2897c1528eD84A218"),
            to_checksum_address("0x236aa50979D5f3De3Bd1Eeb40E81137F22ab794b"),
            symbol="mtBTC",
            underlying_symbol="tBTC",
        ),
        "LBTC": _market(
            to_checksum_address("0x10fF57877b79e9bd949B3815220eC87B9fc5D2ee"),
            to_checksum_address("0xecAc9C5F704e954931349Da37F60E39f515c11c1"),
            symbol="mLBTC",
            underlying_symbol="LBTC",
        ),
        "VIRTUAL": _market(
            to_checksum_address("0xdE8Df9d942D78edE3Ca06e60712582F79CFfFC64"),
            to_checksum_address("0x0b3e328455c4059EEb9e3f84b5543F74E24e7E1b"),
            symbol="mVIRTUAL",
            underlying_symbol="VIRTUAL",
        ),
        "MORPHO": _market(
            to_checksum_address("0x6308204872BdB7432dF97b04B42443c714904F3E"),
            to_checksum_address("0xBAa5CC21fd487B8Fcc2F632f3F4E8D37262a0842"),
            symbol="mMORPHO",
            underlying_symbol="MORPHO",
        ),
        "cbXRP": _market(
            to_checksum_address("0xb4fb8fed5b3AaA8434f0B19b1b623d977e07e86d"),
            to_checksum_address("0xcb585250f852C6c6bf90434AB21A00f02833a4af"),
            symbol="mcbXRP",
            underlying_symbol="cbXRP",
        ),
        "MAMO": _market(
            to_checksum_address("0x2f90bb22eb3979f5ffad31ea6c3f0792ca66da32"),
            to_checksum_address("0x7300B37DfdfAb110d83290A29DfB31B1740219fE"),
            symbol="mMAMO",
            underlying_symbol="MAMO",
        ),
        "VVV": _market(
            to_checksum_address("0xd64bcb70c613a6d1f4d7d57ba64bb4a0767a9682"),
            to_checksum_address("0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf"),
            symbol="mVVV",
            underlying_symbol="VVV",
        ),
    },
    CHAIN_ID_OPTIMISM: {
        "USDC": _market(
            to_checksum_address("0x8E08617b0d66359D73Aa11E11017834C29155525"),
            to_checksum_address("0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85"),
            symbol="mUSDC",
            underlying_symbol="USDC",
        ),
        "ETH": _market(
            to_checksum_address("0xb4104C02BBf4E9be85AAa41a62974E4e28D59A33"),
            to_checksum_address("0x4200000000000000000000000000000000000006"),
            symbol="mWETH",
            underlying_symbol="WETH",
        ),
        "cbETH": _market(
            to_checksum_address("0x95C84F369bd0251ca903052600A3C96838D78bA1"),
            to_checksum_address("0xadDb6A0412DE1BA0F936DCaeb8Aaa24578dcF3B2"),
            symbol="mcbETH",
            underlying_symbol="cbETH",
        ),
        "wstETH": _market(
            to_checksum_address("0xbb3b1aB66eFB43B10923b87460c0106643B83f9d"),
            to_checksum_address("0x1F32b1c2345538c0c6f582fCB022739c4A194Ebb"),
            symbol="mwstETH",
            underlying_symbol="wstETH",
        ),
        "rETH": _market(
            to_checksum_address("0x4c2E35E3eC4A0C82849637BC04A4609Dbe53d321"),
            to_checksum_address("0x9Bcef72be871e61ED4fBbc7630889beE758eb81D"),
            symbol="mrETH",
            underlying_symbol="rETH",
        ),
        "weETH": _market(
            to_checksum_address("0xb8051464C8c92209C92F3a4CD9C73746C4c3CFb3"),
            to_checksum_address("0x5A7fACB970D094B6C7FF1df0eA68D99E6e73CBFF"),
            symbol="mweETH",
            underlying_symbol="weETH",
        ),
        "WBTC": _market(
            to_checksum_address("0x6e6CA598A06E609c913551B729a228B023f06fDB"),
            to_checksum_address("0x68f180fcCe6836688e9084f035309E29Bf0A2095"),
            symbol="mWBTC",
            underlying_symbol="WBTC",
        ),
        "USDT": _market(
            to_checksum_address("0xa3A53899EE8f9f6E963437C5B3f805FEc538BF84"),
            to_checksum_address("0x94b008aA00579c1307B0EF2c499aD98a8ce58e58"),
            symbol="mUSDT",
            underlying_symbol="USDT",
        ),
        "VELO": _market(
            to_checksum_address("0x866b838b97Ee43F2c818B3cb5Cc77A0dc22003Fc"),
            to_checksum_address("0x9560e827af36c94d2ac33a39bce1fe78631088db"),
            symbol="mVELO",
            underlying_symbol="VELO",
        ),
        "DAI": _market(
            to_checksum_address("0x3FE782C2Fe7668C2F1Eb313ACf3022a31feaD6B2"),
            to_checksum_address("0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1"),
            symbol="mDAI",
            underlying_symbol="DAI",
        ),
        "OP": _market(
            to_checksum_address("0x9fc345a20541Bf8773988515c5950eD69aF01847"),
            to_checksum_address("0x4200000000000000000000000000000000000042"),
            symbol="mOP",
            underlying_symbol="OP",
        ),
        "wrsETH": _market(
            to_checksum_address("0x181bA797ccF779D8aB339721ED6ee827E758668e"),
            to_checksum_address("0x87eEE96D50Fb761AD85B1c982d28A042169d61b1"),
            symbol="mwrsETH",
            underlying_symbol="wrsETH",
        ),
        "USDT0": _market(
            to_checksum_address("0xed37cD7872c6fe4020982d35104bE7919b8f8b33"),
            to_checksum_address("0x01bFF41798a0BcF287b996046Ca68b395DbC1071"),
            symbol="mUSDT0",
            underlying_symbol="USDT0",
        ),
    },
    CHAIN_ID_MOONBEAM: {
        "GLMR": _market(
            to_checksum_address("0x091608f4e4a15335145be0A279483C0f8E4c7955"),
            ZERO_ADDRESS,
            symbol="mGLMR",
            underlying_symbol="GLMR",
            native=True,
        ),
        "xcDOT": _market(
            to_checksum_address("0xD22Da948c0aB3A27f5570b604f3ADef5F68211C3"),
            to_checksum_address("0xffffffff1fcacbd218edc0eba20fc2308c778080"),
            symbol="mDOT",
            underlying_symbol="xcDOT",
            bad_debt=True,
        ),
        "FRAX": _market(
            to_checksum_address("0x1C55649f73CDA2f72CEf3DD6C5CA3d49EFcF484C"),
            to_checksum_address("0x322e86852e492a7ee17f28a78c663da38fb33bfb"),
            symbol="mFRAX",
            underlying_symbol="FRAX",
            bad_debt=True,
        ),
        "xcUSDC": _market(
            to_checksum_address("0x22b1a40e3178fe7C7109eFCc247C5bB2B34ABe32"),
            to_checksum_address("0xFFfffffF7D2B0B761Af01Ca8e25242976ac0aD7D"),
            symbol="mxcUSDC",
            underlying_symbol="xcUSDC",
        ),
        "xcUSDT": _market(
            to_checksum_address("0x42A96C0681B74838eC525AdbD13c37f66388f289"),
            to_checksum_address("0xFFFFFFfFea09FB06d082fd1275CD48b191cbCD1d"),
            symbol="mxcUSDT",
            underlying_symbol="xcUSDT",
        ),
        "ETH_NOMAD": _market(
            to_checksum_address("0xc3090f41Eb54A7f18587FD6651d4D3ab477b07a4"),
            to_checksum_address("0x30d2a9f5fdf90ace8c17952cbb4ee48a55d916a7"),
            symbol="mETH",
            underlying_symbol="ETH.mad",
            deprecated=True,
        ),
        "BTC_NOMAD": _market(
            to_checksum_address("0x24A9d8f1f350d59cB0368D3d52A77dB29c833D1D"),
            to_checksum_address("0x1DC78Acda13a8BC4408B207c9E48CDBc096D95e0"),
            symbol="mWBTC",
            underlying_symbol="BTC.mad",
            deprecated=True,
        ),
        "USDC_NOMAD": _market(
            to_checksum_address("0x02e9081DfadD37A852F9a73C4d7d69e615E61334"),
            to_checksum_address("0x8f552a71efe5eefc207bf75485b356a0b3f01ec9"),
            symbol="mUSDC",
            underlying_symbol="USDC.mad",
            deprecated=True,
        ),
        "ETH_WORMHOLE": _market(
            to_checksum_address("0xb6c94b3A378537300387B57ab1cC0d2083f9AeaC"),
            to_checksum_address("0xab3f0245b83feb11d15aaffefd7ad465a59817ed"),
            symbol="mETH.wh",
            underlying_symbol="ETH.wh",
        ),
        "BTC_WORMHOLE": _market(
            to_checksum_address("0xaaa20c5a584a9fECdFEDD71E46DA7858B774A9ce"),
            to_checksum_address("0xe57ebd2d67b462e9926e04a8e33f01cd0d64346d"),
            symbol="mWBTC.wh",
            underlying_symbol="BTC.wh",
        ),
        "USDC_WORMHOLE": _market(
            to_checksum_address("0x744b1756e7651c6D57f5311767EAFE5E931D615b"),
            to_checksum_address("0x931715fee2d06333043d11f658c8ce934ac61d0c"),
            symbol="mUSDC.wh",
            underlying_symbol="USDC.wh",
        ),
        "BUSD_WORMHOLE": _market(
            to_checksum_address("0x298f2E346b82D69a473BF25f329BDF869e17dEc8"),
            to_checksum_address("0x692c57641fc054c2ad6551ccc6566eba599de1ba"),
            symbol="mBUSD.wh",
            underlying_symbol="BUSD.wh",
            deprecated=True,
        ),
    },
    CHAIN_ID_MOONRIVER: {
        "MOVR": _market(
            to_checksum_address("0x6a1A771C7826596652daDC9145fEAaE62b1cd07f"),
            ZERO_ADDRESS,
            symbol="mMOVR",
            underlying_symbol="MOVR",
            deprecated=True,
            native=True,
        ),
        "xcKSM": _market(
            to_checksum_address("0xa0D116513Bd0B8f3F14e6Ea41556c6Ec34688e0f"),
            to_checksum_address("0xffffffff1fcacbd218edc0eba20fc2308c778080"),
            symbol="mxcKSM",
            underlying_symbol="xcKSM",
            deprecated=True,
        ),
        "FRAX": _market(
            to_checksum_address("0x93Ef8B7c6171BaB1C0A51092B2c9da8dc2ba0e9D"),
            to_checksum_address("0x1A93B23281CC1CDE4C4741353F3064709A16197d"),
            symbol="mFRAX",
            underlying_symbol="FRAX",
            deprecated=True,
        ),
        "BTC": _market(
            to_checksum_address("0x6E745367F4Ad2b3da7339aee65dC85d416614D90"),
            to_checksum_address("0x6aB6d61428fde76768D7b45D8BFeec19c6eF91A8"),
            symbol="mWBTC",
            underlying_symbol="BTC",
            deprecated=True,
        ),
        "USDC": _market(
            to_checksum_address("0xd0670AEe3698F66e2D4dAf071EB9c690d978BFA8"),
            to_checksum_address("0xE3F5a90F9cb311505cd691a46596599aA1A0AD7D"),
            symbol="mUSDC",
            underlying_symbol="USDC",
            deprecated=True,
        ),
        "ETH": _market(
            to_checksum_address("0x6503D905338e2ebB550c9eC39Ced525b612E77aE"),
            to_checksum_address("0x639A647fbe20b6c8ac19E48E2de44ea792c62c5C"),
            symbol="mETH",
            underlying_symbol="ETH",
            deprecated=True,
        ),
        "USDT": _market(
            to_checksum_address("0x36918B66F9A3eC7a59d0007D8458DB17bDffBF21"),
            to_checksum_address("0xB44a9B6905aF7c801311e8F4E76932ee959c663C"),
            symbol="mUSDT",
            underlying_symbol="USDT",
            deprecated=True,
        ),
    },
}

MOONWELL_CORE_MARKETS_BY_MTOKEN: dict[int, dict[str, dict[str, Any]]] = {
    chain_id: {str(market["mtoken"]): market for market in markets.values()}
    for chain_id, markets in MOONWELL_CORE_MARKETS_BY_CHAIN.items()
}

MOONWELL_CHAIN_IDS = tuple(MOONWELL_BY_CHAIN.keys())
