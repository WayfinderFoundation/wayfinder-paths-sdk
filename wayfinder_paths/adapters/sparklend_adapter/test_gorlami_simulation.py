from __future__ import annotations

import httpx
import pytest
from eth_account import Account

from wayfinder_paths.adapters.sparklend_adapter.adapter import SparkLendAdapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_ETHEREUM
from wayfinder_paths.core.utils import web3 as web3_utils
from wayfinder_paths.testing.gorlami import gorlami_configured

pytestmark = pytest.mark.skipif(
    not gorlami_configured(),
    reason="api_key not configured (needed for gorlami fork proxy)",
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chain_id", "usdc", "native_fund", "native_supply", "usdc_fund"),
    [
        (
            CHAIN_ID_ETHEREUM,
            "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",  # USDC
            5 * 10**18,
            int(0.05 * 10**18),
            2_000 * 10**6,
        ),
    ],
)
async def test_gorlami_sparklend_supply_borrow_repay_withdraw_claim(
    gorlami,
    chain_id: int,
    usdc: str,
    native_fund: int,
    native_supply: int,
    usdc_fund: int,
):
    acct = Account.create()

    async def sign_cb(tx: dict) -> bytes:
        signed = acct.sign_transaction(tx)
        return signed.raw_transaction

    # Trigger fork creation (gorlami fixture patches web3_from_chain_id).
    try:
        async with web3_utils.web3_from_chain_id(chain_id) as web3:
            assert await web3.eth.chain_id == int(chain_id)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status is not None and status >= 500:
            pytest.skip(
                f"gorlami could not create fork for chain_id={chain_id} (HTTP {status})"
            )
        raise

    fork_info = gorlami.forks.get(str(chain_id))
    assert fork_info is not None

    # Fund test wallet on the fork.
    await gorlami.set_native_balance(fork_info["fork_id"], acct.address, native_fund)
    await gorlami.set_erc20_balance(
        fork_info["fork_id"],
        usdc,
        acct.address,
        usdc_fund,
    )

    adapter = SparkLendAdapter(
        config={},
        sign_callback=sign_cb,
        wallet_address=acct.address,
    )

    ok, markets = await adapter.get_all_markets(chain_id=chain_id, include_caps=True)
    assert ok is True, markets
    assert isinstance(markets, list) and markets

    # Basic ERC20 supply/withdraw.
    ok, tx = await adapter.lend(chain_id=chain_id, underlying_token=usdc, qty=5 * 10**6)
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    ok, tx = await adapter.unlend(
        chain_id=chain_id, underlying_token=usdc, qty=0, withdraw_full=True
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    # Native supply, enable collateral, borrow USDC, repay-full, withdraw-full.
    ok, tx = await adapter.lend(chain_id=chain_id, qty=native_supply, native=True)
    assert ok is True, tx
    assert (
        isinstance(tx, dict)
        and tx["wrap_tx"].startswith("0x")
        and tx["supply_tx"].startswith("0x")
    )

    wrapped = await adapter._wrapped_native(chain_id=chain_id)
    ok, tx = await adapter.set_collateral(
        chain_id=chain_id, underlying_token=wrapped, use_as_collateral=True
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    ok, tx = await adapter.borrow(chain_id=chain_id, asset=usdc, amount=10 * 10**6)
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    ok, state = await adapter.get_full_user_state(
        chain_id=chain_id, account=acct.address
    )
    assert ok is True, state
    assert any(
        str(p.get("underlying") or "").lower() == wrapped.lower()
        and int(p.get("supply_raw") or 0) > 0
        for p in state.get("positions") or []
    )
    assert any(
        str(p.get("underlying") or "").lower() == usdc.lower()
        and int(p.get("variable_borrow_raw") or 0) > 0
        for p in state.get("positions") or []
    )

    ok, tx = await adapter.repay(
        chain_id=chain_id, asset=usdc, amount=0, repay_full=True
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    ok, pos = await adapter.get_pos(chain_id=chain_id, asset=usdc, account=acct.address)
    assert ok is True, pos
    assert int(pos.get("variable_borrow_raw") or 0) == 0

    ok, tx = await adapter.unlend(
        chain_id=chain_id, qty=0, native=True, withdraw_full=True
    )
    assert ok is True, tx
    assert (
        isinstance(tx, dict)
        and tx["withdraw_tx"].startswith("0x")
        and tx["unwrap_tx"].startswith("0x")
    )

    # Claim rewards (may be zero, but should be callable).
    ok, tx = await adapter.claim_rewards(chain_id=chain_id)
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")
