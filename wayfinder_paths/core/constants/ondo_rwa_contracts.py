from __future__ import annotations

from typing import Any, Literal

from eth_utils import to_checksum_address

from wayfinder_paths.core.constants.chains import (
    CHAIN_ID_ARBITRUM,
    CHAIN_ID_ETHEREUM,
    CHAIN_ID_MANTLE,
    CHAIN_ID_POLYGON,
)

ONDO_RWA_PROTOCOL = "ondo_rwa"
ONDO_SHARES_MULTIPLIER = 10_000

ProductName = Literal["ousg", "rousg", "usdy", "rusdy", "musd"]
FamilyName = Literal["ousg", "usdy"]


def _cs(addr: str) -> str:
    return to_checksum_address(addr)


CHAIN_ID_TO_NAME: dict[int, str] = {
    CHAIN_ID_ETHEREUM: "ethereum",
    CHAIN_ID_POLYGON: "polygon",
    CHAIN_ID_ARBITRUM: "arbitrum",
    CHAIN_ID_MANTLE: "mantle",
}

ONDO_STABLECOINS_ETHEREUM: dict[str, dict[str, Any]] = {
    "usdc": {
        "address": _cs("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"),
        "decimals": 6,
        "symbol": "USDC",
    },
    "pyusd": {
        "address": _cs("0x6c3ea9036406852006290770BEdFcAbA0e23A0e8"),
        "decimals": 6,
        "symbol": "PYUSD",
    },
    "rlusd": {
        "address": _cs("0x8292Bb45bf1Ee4d140127049757C2E0fF06317eD"),
        "decimals": 18,
        "symbol": "RLUSD",
    },
}

ONDO_RWA_MARKETS: dict[tuple[str, int], dict[str, Any]] = {
    ("ousg", CHAIN_ID_ETHEREUM): {
        "product": "ousg",
        "family": "ousg",
        "chain_id": CHAIN_ID_ETHEREUM,
        "chain_name": "ethereum",
        "token": _cs("0x1B19C19393e2d034D8Ff31ff34c81252FcBbee92"),
        "rebasing_token": _cs("0x54043c656F0FAd0652D9Ae2603cDF347c5578d00"),
        "manager": _cs("0x93358db73B6cd4b98D89c8F5f230E81a95c2643a"),
        "oracle": _cs("0x9Cad45a8BF0Ed41Ff33074449B357C7a1fAb4094"),
        "id_registry": _cs("0xcf6958D69d535FD03BD6Df3F4fe6CDcd127D97df"),
        "manual_redemption_recipient": _cs(
            "0x72Be8C14B7564f7a61ba2f6B7E50D18DC1D4B63D"
        ),
        "manual_pyusd_recipient": _cs(
            "0x0317a350b093F8010837d1b844292555d73ebC2c"
        ),
        "stablecoins": ONDO_STABLECOINS_ETHEREUM,
        "minimum_subscribe_value_1e18": 5_000 * 10**18,
        "minimum_redeem_value_1e18": 5_000 * 10**18,
        "supports_subscribe": True,
        "supports_redeem": True,
        "supports_wrap": True,
        "supports_unwrap": True,
        "permissioned": True,
        "read_only": False,
        "notes": [
            "Ethereum-only InstantManager subscribe/redeem flow",
            "Qualified-access onboarding required",
        ],
    },
    ("rousg", CHAIN_ID_ETHEREUM): {
        "product": "rousg",
        "family": "ousg",
        "chain_id": CHAIN_ID_ETHEREUM,
        "chain_name": "ethereum",
        "token": _cs("0x54043c656F0FAd0652D9Ae2603cDF347c5578d00"),
        "underlying_token": _cs("0x1B19C19393e2d034D8Ff31ff34c81252FcBbee92"),
        "manager": _cs("0x93358db73B6cd4b98D89c8F5f230E81a95c2643a"),
        "oracle": _cs("0x9Cad45a8BF0Ed41Ff33074449B357C7a1fAb4094"),
        "id_registry": _cs("0xcf6958D69d535FD03BD6Df3F4fe6CDcd127D97df"),
        "minimum_subscribe_value_1e18": 5_000 * 10**18,
        "minimum_redeem_value_1e18": 5_000 * 10**18,
        "supports_subscribe": True,
        "supports_redeem": True,
        "supports_wrap": True,
        "supports_unwrap": True,
        "permissioned": True,
        "read_only": False,
        "notes": [
            "Rebasing OUSG wrapper on Ethereum",
            "Wrap/unwrap uses the rOUSG token contract directly",
        ],
    },
    ("usdy", CHAIN_ID_ETHEREUM): {
        "product": "usdy",
        "family": "usdy",
        "chain_id": CHAIN_ID_ETHEREUM,
        "chain_name": "ethereum",
        "token": _cs("0x96F6eF951840721AdBF46Ac996b59E0235CB985C"),
        "rebasing_token": _cs("0xaf37c1167910ebC994e266949387d2c7C326b879"),
        "manager": _cs("0xa42613C243b67BF6194Ac327795b926B4b491f15"),
        "oracle": _cs("0xA0219AA5B31e65Bc920B5b6DFb8EdF0988121De0"),
        "blocklist": _cs("0xd8c8174691d936E2C80114EC449037b13421B0a8"),
        "usdyc": _cs("0xe86845788d6e3e5c2393ade1a051ae617d974c09"),
        "stablecoins": ONDO_STABLECOINS_ETHEREUM,
        "minimum_subscribe_value_1e18": 10**18,
        "minimum_redeem_value_1e18": 10**18,
        "supports_subscribe": True,
        "supports_redeem": True,
        "supports_wrap": True,
        "supports_unwrap": True,
        "permissioned": True,
        "read_only": False,
        "notes": [
            "Ethereum-only InstantManager subscribe/redeem flow",
            "Permissioned product for qualifying users",
        ],
    },
    ("rusdy", CHAIN_ID_ETHEREUM): {
        "product": "rusdy",
        "family": "usdy",
        "chain_id": CHAIN_ID_ETHEREUM,
        "chain_name": "ethereum",
        "token": _cs("0xaf37c1167910ebC994e266949387d2c7C326b879"),
        "underlying_token": _cs("0x96F6eF951840721AdBF46Ac996b59E0235CB985C"),
        "manager": _cs("0xa42613C243b67BF6194Ac327795b926B4b491f15"),
        "oracle": _cs("0xA0219AA5B31e65Bc920B5b6DFb8EdF0988121De0"),
        "minimum_subscribe_value_1e18": 10**18,
        "minimum_redeem_value_1e18": 10**18,
        "supports_subscribe": True,
        "supports_redeem": True,
        "supports_wrap": True,
        "supports_unwrap": True,
        "permissioned": True,
        "read_only": False,
        "notes": [
            "Rebasing USDY wrapper on Ethereum",
            "Wrap/unwrap uses the rUSDY token contract directly",
        ],
    },
    ("usdy", CHAIN_ID_MANTLE): {
        "product": "usdy",
        "family": "usdy",
        "chain_id": CHAIN_ID_MANTLE,
        "chain_name": "mantle",
        "token": _cs("0x5bE26527e817998A7206475496fDE1E68957c5A6"),
        "rebasing_token": _cs("0xab575258d37EaA5C8956EfABe71F4eE8F6397cF3"),
        "oracle": _cs("0xA96abbe61AfEdEB0D14a20440Ae7100D9aB4882f"),
        "blocklist": _cs("0xdBd7a7d8807f0C98c9A58f7732f2799c8587e5c6"),
        "supports_subscribe": False,
        "supports_redeem": False,
        "supports_wrap": True,
        "supports_unwrap": True,
        "permissioned": True,
        "read_only": False,
        "notes": [
            "Mantle v1 supports wrapper flow only",
            "No Ethereum-style InstantManager write path",
        ],
    },
    ("musd", CHAIN_ID_MANTLE): {
        "product": "musd",
        "family": "usdy",
        "chain_id": CHAIN_ID_MANTLE,
        "chain_name": "mantle",
        "token": _cs("0xab575258d37EaA5C8956EfABe71F4eE8F6397cF3"),
        "underlying_token": _cs("0x5bE26527e817998A7206475496fDE1E68957c5A6"),
        "oracle": _cs("0xA96abbe61AfEdEB0D14a20440Ae7100D9aB4882f"),
        "supports_subscribe": False,
        "supports_redeem": False,
        "supports_wrap": True,
        "supports_unwrap": True,
        "permissioned": True,
        "read_only": False,
        "notes": [
            "Mantle rebasing USDY wrapper",
            "Uses the same share-based surface as Ethereum rUSDY",
        ],
    },
    ("ousg", CHAIN_ID_POLYGON): {
        "product": "ousg",
        "family": "ousg",
        "chain_id": CHAIN_ID_POLYGON,
        "chain_name": "polygon",
        "token": _cs("0xbA11C5effA33c4D6F8f593CFA394241CfE925811"),
        "cash_manager": _cs("0x6B7443808ACFCD48f1DE212C2557462fA86Ee945"),
        "registry": _cs("0x7cD852c0D7613aA869e632929560f310D4059AC1"),
        "supports_subscribe": False,
        "supports_redeem": False,
        "supports_wrap": False,
        "supports_unwrap": False,
        "permissioned": True,
        "read_only": True,
        "notes": [
            "Polygon OUSG is treated as read-only in v1",
            "Ethereum mainnet remains the verified InstantManager write path",
        ],
    },
    ("usdy", CHAIN_ID_ARBITRUM): {
        "product": "usdy",
        "family": "usdy",
        "chain_id": CHAIN_ID_ARBITRUM,
        "chain_name": "arbitrum",
        "token": _cs("0x35e050d3C0eC2d29D269a8EcEa763a183bDF9A9D"),
        "supports_subscribe": False,
        "supports_redeem": False,
        "supports_wrap": False,
        "supports_unwrap": False,
        "permissioned": True,
        "read_only": True,
        "notes": [
            "Arbitrum USDY is treated as read-only in v1",
        ],
    },
}

ONDO_PRODUCT_DEFAULT_CHAIN: dict[str, int] = {
    "ousg": CHAIN_ID_ETHEREUM,
    "rousg": CHAIN_ID_ETHEREUM,
    "usdy": CHAIN_ID_ETHEREUM,
    "rusdy": CHAIN_ID_ETHEREUM,
    "musd": CHAIN_ID_MANTLE,
}
