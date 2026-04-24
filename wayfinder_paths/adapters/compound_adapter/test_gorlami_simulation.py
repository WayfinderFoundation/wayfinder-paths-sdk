from __future__ import annotations

import httpx
import pytest
from eth_account import Account

from wayfinder_paths.adapters.compound_adapter.adapter import CompoundAdapter
from wayfinder_paths.core.config import get_api_base_url
from wayfinder_paths.core.constants.compound_contracts import COMPOUND_COMET_BY_CHAIN
from wayfinder_paths.core.utils import web3 as web3_utils
from wayfinder_paths.testing.gorlami import gorlami_configured

pytestmark = pytest.mark.skipif(
    not gorlami_configured(),
    reason="api_key not configured (needed for gorlami fork proxy)",
)


CHAIN_PRIORITY = [8453, 42161, 10, 137, 1, 534352, 59144, 130, 2020, 5000]
PREFERRED_COLLATERAL_SYMBOLS = ("WETH", "WSTETH", "WBTC", "CBBTC", "WRON")


def _raw_amount_from_usd(*, price_usd: float | None, decimals: int, usd: float) -> int:
    effective_price = float(price_usd) if price_usd and price_usd > 0 else 1.0
    return max(1, int((float(usd) / effective_price) * float(10**decimals)))


def _pick_collateral_asset(
    market: dict,
    *,
    target_borrow_usd: float,
) -> tuple[dict, int] | None:
    ranked: list[tuple[int, float, str, dict]] = []
    for asset in market.get("collateral_assets") or []:
        price_usd = asset.get("price_usd")
        borrow_cf = float(asset.get("borrow_collateral_factor") or 0)
        remaining_cap = int(asset.get("supply_cap") or 0) - int(
            asset.get("total_supply_asset") or 0
        )
        if (
            price_usd is None
            or float(price_usd) <= 0
            or borrow_cf <= 0
            or remaining_cap <= 0
        ):
            continue
        symbol = str(asset.get("symbol") or "").upper()
        priority = (
            0 if symbol in PREFERRED_COLLATERAL_SYMBOLS else 1,
            0 if "ETH" in symbol else 1,
        )
        ranked.append((priority[0] + priority[1], -float(price_usd), symbol, asset))

    for _priority, _neg_price, _symbol, asset in sorted(ranked):
        decimals = int(asset.get("decimals") or 18)
        remaining_cap = int(asset.get("supply_cap") or 0) - int(
            asset.get("total_supply_asset") or 0
        )
        target_usd = max(50_000.0, float(target_borrow_usd) * 25.0)
        amount = _raw_amount_from_usd(
            price_usd=float(asset["price_usd"]),
            decimals=decimals,
            usd=target_usd,
        )
        if amount > remaining_cap:
            amount = max(1, remaining_cap // 2)
        if amount > 0:
            return asset, amount
    return None


async def _select_live_market(adapter: CompoundAdapter) -> tuple[int, str, dict, dict, int]:
    errors: list[str] = []

    for chain_id in CHAIN_PRIORITY:
        chain_entry = COMPOUND_COMET_BY_CHAIN.get(chain_id)
        if not chain_entry:
            continue
        for market_name, market_cfg in (chain_entry.get("markets") or {}).items():
            comet = str(market_cfg["comet"])
            ok, market_or_error = await adapter.get_market(
                chain_id=chain_id,
                comet=comet,
                include_prices=True,
            )
            if not ok or not isinstance(market_or_error, dict):
                errors.append(f"{chain_id}:{market_name}: {market_or_error}")
                continue

            market = dict(market_or_error)
            pause_state = dict(market.get("pause_state") or {})
            if pause_state.get("supply_paused") or pause_state.get("withdraw_paused"):
                continue
            if not market.get("reward_token"):
                continue

            base_decimals = int(market.get("base_token_decimals") or 18)
            base_price_usd = market.get("base_token_price_usd")
            borrow_amount = max(
                int(market.get("base_borrow_min") or 0),
                _raw_amount_from_usd(
                    price_usd=float(base_price_usd)
                    if base_price_usd is not None
                    else None,
                    decimals=base_decimals,
                    usd=100.0,
                ),
            )
            target_borrow_usd = (
                float(borrow_amount) / float(10**base_decimals)
            ) * float(base_price_usd or 1.0)
            collateral_pick = _pick_collateral_asset(
                market,
                target_borrow_usd=target_borrow_usd,
            )
            if not collateral_pick:
                continue
            collateral_asset, collateral_amount = collateral_pick
            return chain_id, comet, market, collateral_asset, collateral_amount

    message = (
        "No active Compound Comet market available for a live supply/borrow/repay "
        "round trip"
    )
    if errors:
        message = f"{message}. Last errors: {' | '.join(errors[-5:])}"
    pytest.skip(message)


@pytest.mark.asyncio
async def test_gorlami_compound_supply_borrow_repay_withdraw_claim(gorlami) -> None:
    acct = Account.create()

    async def sign_cb(tx: dict) -> bytes:
        signed = acct.sign_transaction(tx)
        return signed.raw_transaction

    adapter = CompoundAdapter(
        config={},
        sign_callback=sign_cb,
        wallet_address=acct.address,
    )

    try:
        chain_id, comet, market, collateral_asset_info, collateral_amount = (
            await _select_live_market(adapter)
        )
    except httpx.ConnectError:
        pytest.skip(
            f"gorlami backend unavailable at {get_api_base_url()}/blockchain/gorlami"
        )
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status is not None and status >= 500:
            pytest.skip(
                f"gorlami could not create a usable fork during Compound market discovery (HTTP {status})"
            )
        raise

    async with web3_utils.web3_from_chain_id(chain_id) as web3:
        assert await web3.eth.chain_id == int(chain_id)

    fork_info = gorlami.forks.get(str(chain_id))
    assert fork_info is not None

    base_token = str(market["base_token"])
    base_decimals = int(market["base_token_decimals"])
    base_borrow_min = int(market["base_borrow_min"])
    base_price_usd = market.get("base_token_price_usd")
    supply_base_amount = max(
        1,
        _raw_amount_from_usd(
            price_usd=float(base_price_usd) if base_price_usd is not None else None,
            decimals=base_decimals,
            usd=1_000.0,
        ),
    )
    borrow_amount = max(
        base_borrow_min,
        _raw_amount_from_usd(
            price_usd=float(base_price_usd) if base_price_usd is not None else None,
            decimals=base_decimals,
            usd=100.0,
        ),
    )
    collateral_asset = str(collateral_asset_info["asset"])
    base_balance_buffer = (borrow_amount * 3) + (supply_base_amount * 2)

    await gorlami.set_native_balance(fork_info["fork_id"], acct.address, 10 * 10**18)
    await gorlami.set_erc20_balance(
        fork_info["fork_id"],
        base_token,
        acct.address,
        base_balance_buffer,
    )
    await gorlami.set_erc20_balance(
        fork_info["fork_id"],
        collateral_asset,
        acct.address,
        collateral_amount,
    )

    ok, tx = await adapter.lend(
        chain_id=chain_id,
        comet=comet,
        base_token=base_token,
        amount=supply_base_amount,
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    ok, tx = await adapter.unlend(
        chain_id=chain_id,
        comet=comet,
        base_token=base_token,
        amount=0,
        withdraw_full=True,
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    ok, tx = await adapter.supply_collateral(
        chain_id=chain_id,
        comet=comet,
        collateral_asset=collateral_asset,
        amount=collateral_amount,
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    ok, tx = await adapter.borrow(
        chain_id=chain_id,
        comet=comet,
        base_token=base_token,
        amount=borrow_amount,
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    ok, pos = await adapter.get_pos(
        chain_id=chain_id,
        comet=comet,
        account=acct.address,
        include_prices=True,
    )
    assert ok is True, pos
    assert isinstance(pos, dict)
    assert int(pos["borrowed_base"]) > 0
    assert any(
        str(item.get("asset") or "").lower() == collateral_asset.lower()
        and int(item.get("balance") or 0) > 0
        for item in pos["collateral_positions"]
    )

    ok, tx = await adapter.repay(
        chain_id=chain_id,
        comet=comet,
        base_token=base_token,
        amount=0,
        repay_full=True,
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    ok, pos = await adapter.get_pos(
        chain_id=chain_id,
        comet=comet,
        account=acct.address,
        include_prices=True,
    )
    assert ok is True, pos
    assert isinstance(pos, dict)
    assert int(pos["borrowed_base"]) == 0

    ok, tx = await adapter.withdraw_collateral(
        chain_id=chain_id,
        comet=comet,
        collateral_asset=collateral_asset,
        amount=0,
        withdraw_full=True,
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    ok, pos = await adapter.get_pos(
        chain_id=chain_id,
        comet=comet,
        account=acct.address,
        include_prices=True,
    )
    assert ok is True, pos
    assert isinstance(pos, dict)
    assert all(
        int(item.get("balance") or 0) == 0 for item in pos["collateral_positions"]
    )

    ok, tx = await adapter.claim_rewards(chain_id=chain_id, comet=comet)
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")
