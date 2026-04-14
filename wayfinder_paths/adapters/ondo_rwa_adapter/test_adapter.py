from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.adapters.ondo_rwa_adapter.adapter import OndoRwaAdapter
from wayfinder_paths.core.constants.ondo_rwa_contracts import ONDO_RWA_MARKETS

WALLET = "0x1234567890123456789012345678901234567890"
PATCH_PREFIX = "wayfinder_paths.adapters.ondo_rwa_adapter.adapter"


@pytest.fixture
def adapter() -> OndoRwaAdapter:
    return OndoRwaAdapter(
        config={},
        sign_callback=AsyncMock(return_value=b"\x00" * 32),
        wallet_address=WALLET,
    )


def test_adapter_type() -> None:
    adapter = OndoRwaAdapter(config={})
    assert adapter.adapter_type == "ONDO_RWA"
    assert adapter.name == "ondo_rwa_adapter"


def test_wallet_is_checksummed() -> None:
    adapter = OndoRwaAdapter(
        config={},
        wallet_address="0xabcdefabcdefabcdefabcdefabcdefabcdefabcd",
    )
    assert adapter.wallet_address == "0xABcdEFABcdEFabcdEfAbCdefabcdeFABcDEFabCD"


def test_family_name_accepts_product_aliases() -> None:
    adapter = OndoRwaAdapter(config={})
    assert adapter._family_name("rousg") == "ousg"
    assert adapter._family_name("rusdy") == "usdy"


@pytest.mark.asyncio
async def test_get_pos_requires_account_or_wallet() -> None:
    adapter = OndoRwaAdapter(config={})
    ok, msg = await adapter.get_pos()
    assert ok is False
    assert msg == "account (or wallet_address) is required"


@pytest.mark.asyncio
async def test_is_registered_or_eligible_is_read_only_for_non_ousg() -> None:
    adapter = OndoRwaAdapter(config={})
    ok, data = await adapter.is_registered_or_eligible(
        account=WALLET,
        product_family="usdy",
    )
    assert ok is True
    assert data["supported"] is False
    assert data["eligible"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("product", "expected_fn", "market_key"),
    [
        ("ousg", "subscribe", ("ousg", 1)),
        ("rousg", "subscribeRebasingOUSG", ("rousg", 1)),
        ("usdy", "subscribe", ("usdy", 1)),
        ("rusdy", "subscribeRebasingUSDY", ("rusdy", 1)),
    ],
)
async def test_subscribe_routes_to_expected_manager_function(
    adapter: OndoRwaAdapter,
    product: str,
    expected_fn: str,
    market_key: tuple[str, int],
) -> None:
    market = ONDO_RWA_MARKETS[market_key]
    family_market = ONDO_RWA_MARKETS[(str(market["family"]), int(market["chain_id"]))]
    deposit_token = family_market["stablecoins"]["usdc"]["address"]

    with (
        patch.object(
            OndoRwaAdapter,
            "is_subscription_token_supported",
            new_callable=AsyncMock,
            return_value=(True, True),
        ),
        patch(
            f"{PATCH_PREFIX}.ensure_allowance",
            new_callable=AsyncMock,
            return_value=(True, {}),
        ) as mock_allowance,
        patch.object(
            OndoRwaAdapter,
            "_preflight_transaction",
            new_callable=AsyncMock,
            return_value=(True, None),
        ),
        patch(
            f"{PATCH_PREFIX}.encode_call",
            new_callable=AsyncMock,
            return_value={
                "chainId": market["chain_id"],
                "from": WALLET,
                "to": market["manager"],
                "data": "0xdeadbeef",
                "value": 0,
            },
        ) as mock_encode,
        patch(
            f"{PATCH_PREFIX}.send_transaction",
            new_callable=AsyncMock,
            return_value="0xsub",
        ),
    ):
        ok, result = await adapter.subscribe(
            product=product,
            deposit_token=deposit_token,
            amount=10_000_000_000,
            min_received=1,
        )

    assert ok is True
    assert result == "0xsub"
    allow_kwargs = mock_allowance.await_args.kwargs
    assert allow_kwargs["token_address"] == deposit_token
    assert allow_kwargs["spender"] == market["manager"]

    encode_kwargs = mock_encode.await_args.kwargs
    assert encode_kwargs["target"] == market["manager"]
    assert encode_kwargs["fn_name"] == expected_fn


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("product", "expected_fn", "market_key"),
    [
        ("ousg", "redeem", ("ousg", 1)),
        ("rousg", "redeemRebasingOUSG", ("rousg", 1)),
        ("usdy", "redeem", ("usdy", 1)),
        ("rusdy", "redeemRebasingUSDY", ("rusdy", 1)),
    ],
)
async def test_redeem_routes_to_expected_manager_function(
    adapter: OndoRwaAdapter,
    product: str,
    expected_fn: str,
    market_key: tuple[str, int],
) -> None:
    market = ONDO_RWA_MARKETS[market_key]
    family_market = ONDO_RWA_MARKETS[(str(market["family"]), int(market["chain_id"]))]
    receiving_token = family_market["stablecoins"]["usdc"]["address"]

    with (
        patch(
            f"{PATCH_PREFIX}.ensure_allowance",
            new_callable=AsyncMock,
            return_value=(True, {}),
        ) as mock_allowance,
        patch.object(
            OndoRwaAdapter,
            "_preflight_transaction",
            new_callable=AsyncMock,
            return_value=(True, None),
        ),
        patch(
            f"{PATCH_PREFIX}.encode_call",
            new_callable=AsyncMock,
            return_value={
                "chainId": market["chain_id"],
                "from": WALLET,
                "to": market["manager"],
                "data": "0xfeedface",
                "value": 0,
            },
        ) as mock_encode,
        patch(
            f"{PATCH_PREFIX}.send_transaction",
            new_callable=AsyncMock,
            return_value="0xred",
        ),
    ):
        ok, result = await adapter.redeem(
            product=product,
            amount=2 * 10**18,
            receiving_token=receiving_token,
            min_received=1,
        )

    assert ok is True
    assert result == "0xred"
    allow_kwargs = mock_allowance.await_args.kwargs
    assert allow_kwargs["token_address"] == market["token"]
    assert allow_kwargs["spender"] == market["manager"]

    encode_kwargs = mock_encode.await_args.kwargs
    assert encode_kwargs["target"] == market["manager"]
    assert encode_kwargs["fn_name"] == expected_fn


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("product", "chain_id", "expected_wrapper"),
    [
        ("ousg", 1, ONDO_RWA_MARKETS[("rousg", 1)]["token"]),
        ("usdy", 1, ONDO_RWA_MARKETS[("rusdy", 1)]["token"]),
    ],
)
async def test_wrap_routes_to_rebasing_wrapper(
    adapter: OndoRwaAdapter,
    product: str,
    chain_id: int,
    expected_wrapper: str,
) -> None:
    with (
        patch(
            f"{PATCH_PREFIX}.ensure_allowance",
            new_callable=AsyncMock,
            return_value=(True, {}),
        ) as mock_allowance,
        patch.object(
            OndoRwaAdapter,
            "_preflight_transaction",
            new_callable=AsyncMock,
            return_value=(True, None),
        ),
        patch(
            f"{PATCH_PREFIX}.encode_call",
            new_callable=AsyncMock,
            return_value={
                "chainId": chain_id,
                "from": WALLET,
                "to": expected_wrapper,
                "data": "0xwrap",
                "value": 0,
            },
        ) as mock_encode,
        patch(
            f"{PATCH_PREFIX}.send_transaction",
            new_callable=AsyncMock,
            return_value="0xwrap",
        ),
    ):
        ok, result = await adapter.wrap(
            product=product,
            chain_id=chain_id,
            amount=10**18,
        )

    assert ok is True
    assert result == "0xwrap"
    assert mock_allowance.await_args.kwargs["spender"] == expected_wrapper
    assert mock_encode.await_args.kwargs["target"] == expected_wrapper
    assert mock_encode.await_args.kwargs["fn_name"] == "wrap"


@pytest.mark.asyncio
async def test_wrap_infers_market_from_token_address(adapter: OndoRwaAdapter) -> None:
    token_address = ONDO_RWA_MARKETS[("usdy", 1)]["token"]
    wrapper = ONDO_RWA_MARKETS[("rusdy", 1)]["token"]

    with (
        patch(
            f"{PATCH_PREFIX}.ensure_allowance",
            new_callable=AsyncMock,
            return_value=(True, {}),
        ),
        patch.object(
            OndoRwaAdapter,
            "_preflight_transaction",
            new_callable=AsyncMock,
            return_value=(True, None),
        ),
        patch(
            f"{PATCH_PREFIX}.encode_call",
            new_callable=AsyncMock,
            return_value={
                "chainId": 1,
                "from": WALLET,
                "to": wrapper,
                "data": "0xwrap",
                "value": 0,
            },
        ) as mock_encode,
        patch(
            f"{PATCH_PREFIX}.send_transaction",
            new_callable=AsyncMock,
            return_value="0xwrap",
        ),
    ):
        ok, result = await adapter.wrap(amount=10**18, token_address=token_address)

    assert ok is True
    assert result == "0xwrap"
    assert mock_encode.await_args.kwargs["target"] == wrapper


@pytest.mark.asyncio
async def test_wrap_requires_product_or_token_address(
    adapter: OndoRwaAdapter,
) -> None:
    ok, msg = await adapter.wrap(amount=10**18)
    assert ok is False
    assert msg == "Either product or token_address is required"


@pytest.mark.asyncio
async def test_unwrap_requires_product_or_token_address(
    adapter: OndoRwaAdapter,
) -> None:
    ok, msg = await adapter.unwrap(amount=10**18)
    assert ok is False
    assert msg == "Either product or token_address is required"


@pytest.mark.asyncio
async def test_subscribe_rejects_allowlist_miss(adapter: OndoRwaAdapter) -> None:
    deposit_token = ONDO_RWA_MARKETS[("ousg", 1)]["stablecoins"]["rlusd"]["address"]

    with patch.object(
        OndoRwaAdapter,
        "is_subscription_token_supported",
        new_callable=AsyncMock,
        return_value=(True, False),
    ):
        ok, msg = await adapter.subscribe(
            product="ousg",
            deposit_token=deposit_token,
            amount=10**6,
            min_received=1,
        )

    assert ok is False
    assert "not currently allowlisted" in msg


@pytest.mark.asyncio
async def test_arbitrum_usdy_subscribe_is_not_supported(
    adapter: OndoRwaAdapter,
) -> None:
    ok, msg = await adapter.subscribe(
        product="usdy",
        deposit_token=ONDO_RWA_MARKETS[("usdy", 42161)]["token"],
        amount=10**18,
        min_received=1,
        chain_id=42161,
    )
    assert ok is False
    assert "not supported" in msg.lower()


@pytest.mark.asyncio
async def test_get_full_user_state_aggregates_positions(
    adapter: OndoRwaAdapter,
) -> None:
    with (
        patch.object(
            OndoRwaAdapter,
            "get_pos",
            new_callable=AsyncMock,
            return_value=(
                True,
                {
                    "positions": [
                        {"product": "ousg", "chain_id": 1, "usd_value": 100.0},
                        {"product": "usdy", "chain_id": 42161, "usd_value": 25.0},
                    ],
                    "total_usd_value": 125.0,
                },
            ),
        ),
        patch.object(
            OndoRwaAdapter,
            "is_registered_or_eligible",
            new_callable=AsyncMock,
            return_value=(True, {"supported": True, "eligible": False}),
        ),
    ):
        ok, state = await adapter.get_full_user_state(account=WALLET, include_usd=True)

    assert ok is True
    assert state["total_usd_value"] == 125.0
    assert set(state["positions_by_product"]) == {"ousg", "usdy"}
    assert set(state["positions_by_chain"]) == {"1", "42161"}
    assert state["registration"]["ousg"]["eligible"] is False
