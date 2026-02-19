from __future__ import annotations

import pytest
from eth_account import Account

from wayfinder_paths.adapters.euler_v2_adapter.adapter import EulerV2Adapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE
from wayfinder_paths.core.utils import web3 as web3_utils
from wayfinder_paths.testing.gorlami import gorlami_configured

pytestmark = pytest.mark.skipif(
    not gorlami_configured(),
    reason="api_key not configured (needed for gorlami fork proxy)",
)


def _headroom_amount(*, supply_cap: int, total_assets: int, desired: int) -> int:
    if supply_cap <= 0:
        return desired
    headroom = max(0, int(supply_cap) - int(total_assets))
    if headroom <= 0:
        return 0
    # Keep a cushion in case totals shift slightly between calls.
    return min(int(desired), max(1, headroom // 10))


@pytest.mark.asyncio
async def test_gorlami_euler_v2_deposit_borrow_repay_withdraw(gorlami):
    chain_id = CHAIN_ID_BASE

    acct = Account.create()

    async def sign_cb(tx: dict) -> bytes:
        signed = acct.sign_transaction(tx)
        return signed.raw_transaction

    # Trigger fork creation (gorlami fixture patches web3_from_chain_id).
    async with web3_utils.web3_from_chain_id(chain_id) as web3:
        assert await web3.eth.chain_id == int(chain_id)

    fork_info = gorlami.forks.get(str(chain_id))
    assert fork_info is not None

    # Fund test wallet on the fork.
    await gorlami.set_native_balance(fork_info["fork_id"], acct.address, 2 * 10**18)

    adapter = EulerV2Adapter(
        config={"strategy_wallet": {"address": acct.address}},
        strategy_wallet_signing_callback=sign_cb,
    )

    ok, markets = await adapter.get_all_markets(chain_id=chain_id, limit=60)
    assert ok is True, markets
    assert isinstance(markets, list) and markets

    borrow_vault = None
    collateral_vault = None
    borrow_amount = 0
    deposit_amount = 0
    collateral_asset = None
    borrow_asset = None

    # Find a borrow vault with at least one collateral option and some cash to borrow.
    for m in markets:
        try:
            if int(m.get("cash") or 0) <= 0:
                continue
            ltvs = m.get("collateral_ltv_info") or []
            if not ltvs:
                continue

            candidate_borrow_vault = str(m.get("vault") or "")
            candidate_borrow_asset = str(m.get("underlying") or "")
            borrow_decimals = int(m.get("asset_decimals") or 18)
            cash = int(m.get("cash") or 0)

            # Prefer a collateral vault from the LTV list that is also verified (likely stable).
            for ltv in ltvs[:5]:
                candidate_collateral_vault = str(ltv.get("collateral") or "")
                ok2, cinfo = await adapter.get_vault_info_full(
                    chain_id=chain_id, vault=candidate_collateral_vault
                )
                if not ok2:
                    continue

                supply_cap = int(cinfo.get("supplyCap") or 0)
                total_assets = int(cinfo.get("totalAssets") or 0)
                collateral_decimals = int(cinfo.get("assetDecimals") or 18)
                desired_deposit = 100 * 10**collateral_decimals
                qty_deposit = _headroom_amount(
                    supply_cap=supply_cap, total_assets=total_assets, desired=desired_deposit
                )
                if qty_deposit <= 0:
                    continue

                # Borrow a small amount to avoid price/ltv edge cases.
                qty_borrow = min(max(1, 10**borrow_decimals // 1000), max(1, cash // 100))
                if qty_borrow <= 0:
                    continue

                borrow_vault = candidate_borrow_vault
                collateral_vault = candidate_collateral_vault
                borrow_amount = int(qty_borrow)
                deposit_amount = int(qty_deposit)
                collateral_asset = str(cinfo.get("asset") or "")
                borrow_asset = candidate_borrow_asset
                break

            if borrow_vault and collateral_vault:
                break
        except Exception:  # noqa: BLE001 - continue searching
            continue

    assert borrow_vault and collateral_vault
    assert collateral_asset and borrow_asset
    assert deposit_amount > 0 and borrow_amount > 0

    # Fund ERC20 balances for deposit + repay buffer.
    await gorlami.set_erc20_balance(
        fork_info["fork_id"], collateral_asset, acct.address, deposit_amount * 20
    )
    await gorlami.set_erc20_balance(
        fork_info["fork_id"], borrow_asset, acct.address, borrow_amount * 20
    )

    ok, tx = await adapter.lend(
        chain_id=chain_id, vault=collateral_vault, amount=deposit_amount
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    ok, tx = await adapter.set_collateral(chain_id=chain_id, vault=collateral_vault)
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    ok, tx = await adapter.borrow(
        chain_id=chain_id, vault=borrow_vault, amount=borrow_amount
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    ok, state = await adapter.get_full_user_state(
        chain_id=chain_id, account=acct.address, include_zero_positions=False
    )
    assert ok is True, state
    positions = state.get("positions") or []

    assert any(
        p.get("vault", "").lower() == str(collateral_vault).lower()
        and int(p.get("assets") or 0) > 0
        for p in positions
    )
    assert any(
        p.get("vault", "").lower() == str(borrow_vault).lower()
        and int(p.get("borrowed") or 0) > 0
        for p in positions
    )

    ok, tx = await adapter.repay(
        chain_id=chain_id, vault=borrow_vault, amount=0, repay_full=True
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    ok, state = await adapter.get_full_user_state(
        chain_id=chain_id, account=acct.address, include_zero_positions=True
    )
    assert ok is True, state
    positions = state.get("positions") or []

    assert all(
        int(p.get("borrowed") or 0) == 0
        for p in positions
        if p.get("vault", "").lower() == str(borrow_vault).lower()
    )

    # Collateral removal should succeed once debt is repaid.
    ok, tx = await adapter.remove_collateral(chain_id=chain_id, vault=collateral_vault)
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    ok, tx = await adapter.unlend(
        chain_id=chain_id, vault=collateral_vault, amount=0, withdraw_full=True
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

