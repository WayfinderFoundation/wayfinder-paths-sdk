from __future__ import annotations

from collections.abc import Sequence

import pytest
from eth_account import Account

from wayfinder_paths.adapters.pendle_adapter import PendleAdapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_ARBITRUM, CHAIN_ID_PLASMA
from wayfinder_paths.core.utils import web3 as web3_utils
from wayfinder_paths.core.utils.tokens import get_token_balance, get_token_decimals
from wayfinder_paths.testing.gorlami import gorlami_configured

pytestmark = pytest.mark.skipif(
    not gorlami_configured(),
    reason="api_key not configured (needed for gorlami fork proxy)",
)

TAKER_LIMIT_ORDER_TYPES = (
    "TOKEN_FOR_PT",
    "PT_FOR_TOKEN",
    "TOKEN_FOR_YT",
    "YT_FOR_TOKEN",
)


async def _make_account_and_adapter(
    gorlami,
    chain_id: int,
) -> tuple[PendleAdapter, str, str]:
    acct = Account.create()

    async def sign_cb(tx: dict) -> bytes:
        signed = acct.sign_transaction(tx)
        return signed.raw_transaction

    # Trigger fork creation through the gorlami fixture's patched web3 helper.
    async with web3_utils.web3_from_chain_id(chain_id) as web3:
        assert await web3.eth.chain_id == chain_id

    fork_info = gorlami.forks.get(str(chain_id))
    assert fork_info is not None

    await gorlami.set_native_balance(fork_info["fork_id"], acct.address, 2 * 10**18)

    return (
        PendleAdapter(sign_callback=sign_cb, wallet_address=acct.address),
        acct.address,
        fork_info["fork_id"],
    )


async def _select_pt_market(
    adapter: PendleAdapter,
    *,
    chain_id: int,
    preferred_name_fragments: Sequence[str],
) -> dict:
    rows = await adapter.list_active_pt_yt_markets(
        chain=chain_id,
        min_days_to_expiry=1,
        sort_by="liquidity",
        descending=True,
    )
    rows = [
        row
        for row in rows
        if row.get("marketAddress")
        and row.get("ptAddress")
        and row.get("ytAddress")
        and row.get("liquidityUsd", 0) > 0
    ]
    if not rows:
        pytest.skip(f"No active Pendle PT/YT markets found on chain {chain_id}")

    preferred = tuple(fragment.lower() for fragment in preferred_name_fragments)
    for row in rows:
        name = str(row.get("marketName") or "").lower()
        if any(fragment in name for fragment in preferred):
            return row
    return rows[0]


async def _fetch_first_taker_limit_order_page(
    adapter: PendleAdapter,
    *,
    chain_id: int,
    yt_address: str,
) -> tuple[str, dict]:
    for order_type in TAKER_LIMIT_ORDER_TYPES:
        page = await adapter.fetch_taker_limit_orders(
            chain=chain_id,
            yt=yt_address,
            order_type=order_type,
            skip=0,
            limit=2,
            sort_by="Implied Rate",
            sort_order="asc",
        )
        if page.get("results"):
            return order_type, page
    pytest.skip(f"No live Pendle taker limit orders found for YT {yt_address}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chain_id", "preferred_name_fragments"),
    [
        (CHAIN_ID_ARBITRUM, ("usdc", "usde", "usd", "dai")),
        (CHAIN_ID_PLASMA, ("usde", "usd", "yield")),
    ],
    ids=["arbitrum", "plasma"],
)
async def test_gorlami_pendle_live_market_state_scan(
    gorlami,
    chain_id: int,
    preferred_name_fragments: Sequence[str],
):
    adapter, account, fork_id = await _make_account_and_adapter(gorlami, chain_id)
    selected = await _select_pt_market(
        adapter,
        chain_id=chain_id,
        preferred_name_fragments=preferred_name_fragments,
    )

    async with web3_utils.web3_from_chain_id(chain_id) as web3:
        pt_decimals = await get_token_decimals(
            selected["ptAddress"],
            chain_id,
            web3=web3,
        )

    pt_amount = 3 * 10**pt_decimals
    await gorlami.set_erc20_balance(
        fork_id,
        selected["ptAddress"],
        account,
        pt_amount,
    )

    ok, state = await adapter.get_full_user_state_per_chain(
        chain=chain_id,
        account=account,
        include_inactive=False,
        include_zero_positions=False,
        include_prices=False,
    )
    assert ok is True, state
    assert isinstance(state, dict)

    positions = state.get("positions")
    assert isinstance(positions, list)

    position = next(
        (
            pos
            for pos in positions
            if str(pos.get("marketAddress", "")).lower()
            == selected["marketAddress"].lower()
        ),
        None,
    )
    assert position is not None, {
        "selected": selected,
        "positions": positions,
    }

    balances = position.get("balances") or {}
    pt_balance = balances.get("pt") or {}
    assert int(pt_balance.get("raw") or 0) == pt_amount
    assert int(pt_balance.get("decimals") or -1) == pt_decimals
    assert str(position.get("pt", "")).lower() == selected["ptAddress"].lower()
    assert str(position.get("yt", "")).lower() == selected["ytAddress"].lower()

    order_type, limit_orders = await _fetch_first_taker_limit_order_page(
        adapter,
        chain_id=chain_id,
        yt_address=selected["ytAddress"],
    )
    assert limit_orders["total"] >= len(limit_orders["results"])
    assert limit_orders["limit"] == 2

    order_info = limit_orders["results"][0]
    order = order_info.get("order") or {}
    assert order.get("id", "").startswith("0x")
    assert int(order.get("chainId")) == chain_id
    assert str(order.get("yt", "")).lower() == selected["ytAddress"].lower()
    assert order_type in TAKER_LIMIT_ORDER_TYPES
    assert int(order.get("type")) in (0, 1, 2, 3)
    assert "netFromTaker" in order_info
    assert "netToTaker" in order_info


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chain_id", "preferred_name_fragments"),
    [
        (CHAIN_ID_ARBITRUM, ("usdc", "usde", "usd", "dai")),
        (CHAIN_ID_PLASMA, ("usde", "usd", "yield")),
    ],
    ids=["arbitrum", "plasma"],
)
async def test_gorlami_pendle_taker_limit_order_fill(
    gorlami,
    chain_id: int,
    preferred_name_fragments: Sequence[str],
):
    adapter, account, fork_id = await _make_account_and_adapter(gorlami, chain_id)
    selected = await _select_pt_market(
        adapter,
        chain_id=chain_id,
        preferred_name_fragments=preferred_name_fragments,
    )
    _, limit_orders = await _fetch_first_taker_limit_order_page(
        adapter,
        chain_id=chain_id,
        yt_address=selected["ytAddress"],
    )
    order_info = limit_orders["results"][0]
    order = order_info["order"]
    taking_token = order.get("takingToken")
    if not isinstance(taking_token, str) or not taking_token.startswith("0x"):
        pytest.skip(f"Limit order missing ERC20 takingToken: {order}")

    max_taking = (int(order_info["netFromTaker"]) * 101) // 100
    await gorlami.set_erc20_balance(
        fork_id,
        taking_token,
        account,
        max_taking * 2,
    )
    pre_taking = await get_token_balance(taking_token, chain_id, account)

    ok, result = await adapter.execute_taker_limit_order_fill(
        chain=chain_id,
        limit_order_items=order_info,
    )
    assert ok is True, result
    assert result["tx_hash"].startswith("0x")

    post_taking = int(result["balances"]["post"][taking_token])
    assert post_taking < int(pre_taking)

    output_tokens = [
        token
        for token in (order.get("makingToken"), order.get("sy"))
        if isinstance(token, str)
    ]
    assert any(
        int(result["balances"]["post"].get(token, 0))
        > int(result["balances"]["pre"].get(token, 0))
        for token in output_tokens
    )
