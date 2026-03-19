import pytest

from wayfinder_paths.adapters.aerodrome_adapter.adapter import AerodromeAdapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE


def test_adapter_type():
    adapter = AerodromeAdapter()
    assert adapter.adapter_type == "AERODROME"


def test_constructor_is_base_only():
    adapter = AerodromeAdapter()
    assert adapter.chain_id == CHAIN_ID_BASE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,kwargs",
    [
        (
            "add_liquidity",
            {
                "tokenA": "0x0000000000000000000000000000000000000001",
                "tokenB": "0x0000000000000000000000000000000000000002",
                "stable": False,
                "amountA_desired": 1,
                "amountB_desired": 1,
            },
        ),
        (
            "stake_lp",
            {
                "gauge": "0x0000000000000000000000000000000000000003",
                "amount": 1,
            },
        ),
        (
            "create_lock",
            {
                "amount": 1,
                "lock_duration": 1,
            },
        ),
    ],
)
async def test_require_wallet_returns_false_when_no_wallet(method, kwargs):
    adapter = AerodromeAdapter()
    ok, msg = await getattr(adapter, method)(**kwargs)
    assert ok is False
    assert msg == "wallet address not configured"
