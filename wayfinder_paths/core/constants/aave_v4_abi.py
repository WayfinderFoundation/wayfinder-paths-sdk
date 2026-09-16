"""Minimal ABI for Aave v4 ISpoke/IHub. Deliberately separate from v3."""

RESERVE_FIELDS = [
    ("underlying", "address"),
    ("hub", "address"),
    ("asset_id", "uint16"),
    ("decimals", "uint8"),
    ("collateral_risk", "uint24"),
    ("flags", "uint8"),
    ("dynamic_config_key", "uint32"),
]
CONFIG_FIELDS = [
    ("collateral_risk", "uint24"),
    ("paused", "bool"),
    ("frozen", "bool"),
    ("borrowable", "bool"),
    ("receive_shares_enabled", "bool"),
]
ACCOUNT_FIELDS = [
    (name, "uint256")
    for name in (
        "risk_premium",
        "avg_collateral_factor",
        "health_factor",
        "total_collateral_value",
        "total_debt_value_ray",
        "active_collateral_count",
        "borrow_count",
    )
]
HUB_CONFIG_FIELDS = [
    ("add_cap", "uint40"),
    ("draw_cap", "uint40"),
    ("risk_premium_threshold", "uint24"),
    ("active", "bool"),
    ("halted", "bool"),
]

SPOKE_ABI = (
    [
        {
            "type": "function",
            "name": name,
            "stateMutability": "view",
            "inputs": [{"name": n, "type": t} for n, t in inputs],
            "outputs": [
                {
                    "name": "",
                    "type": "tuple",
                    "components": [{"name": n, "type": t} for n, t in fields],
                }
            ],
        }
        for name, inputs, fields in (
            ("getReserve", [("reserveId", "uint256")], RESERVE_FIELDS),
            ("getReserveConfig", [("reserveId", "uint256")], CONFIG_FIELDS),
            ("getUserAccountData", [("user", "address")], ACCOUNT_FIELDS),
        )
    ]
    + [
        {
            "type": "function",
            "name": name,
            "stateMutability": "view",
            "inputs": [{"name": n, "type": t} for n, t in inputs],
            "outputs": [{"name": "", "type": "uint256"}],
        }
        for name, inputs in (
            ("getReserveCount", []),
            ("getUserSuppliedAssets", [("reserveId", "uint256"), ("user", "address")]),
            ("getUserTotalDebt", [("reserveId", "uint256"), ("user", "address")]),
        )
    ]
    + [
        {
            "type": "function",
            "name": name,
            "stateMutability": "nonpayable",
            "inputs": [
                {"name": "reserveId", "type": "uint256"},
                {"name": "amount", "type": "uint256"},
                {"name": "onBehalfOf", "type": "address"},
            ],
            "outputs": [
                {"name": "", "type": "uint256"},
                {"name": "", "type": "uint256"},
            ],
        }
        for name in ("supply", "withdraw", "borrow", "repay")
    ]
    + [
        {
            "type": "function",
            "name": "setUsingAsCollateral",
            "stateMutability": "nonpayable",
            "inputs": [
                {"name": "reserveId", "type": "uint256"},
                {"name": "usingAsCollateral", "type": "bool"},
                {"name": "onBehalfOf", "type": "address"},
            ],
            "outputs": [],
        }
    ]
)

HUB_ABI = [
    {
        "type": "function",
        "name": name,
        "stateMutability": "view",
        "inputs": [{"name": n, "type": t} for n, t in inputs],
        "outputs": [{"name": "", "type": "uint256"}],
    }
    for name, inputs in (
        ("getAssetLiquidity", [("assetId", "uint256")]),
        ("getSpokeAddedAssets", [("assetId", "uint256"), ("spoke", "address")]),
        ("getSpokeTotalOwed", [("assetId", "uint256"), ("spoke", "address")]),
    )
] + [
    {
        "type": "function",
        "name": "getSpokeConfig",
        "stateMutability": "view",
        "inputs": [
            {"name": "assetId", "type": "uint256"},
            {"name": "spoke", "type": "address"},
        ],
        "outputs": [
            {
                "name": "",
                "type": "tuple",
                "components": [{"name": n, "type": t} for n, t in HUB_CONFIG_FIELDS],
            }
        ],
    }
]
