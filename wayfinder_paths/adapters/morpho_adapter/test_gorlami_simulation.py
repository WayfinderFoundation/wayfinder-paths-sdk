from __future__ import annotations

import pytest
from eth_account import Account

from wayfinder_paths.adapters.morpho_adapter.adapter import MorphoAdapter
from wayfinder_paths.core.clients.MorphoClient import MORPHO_CLIENT
from wayfinder_paths.core.constants.chains import CHAIN_ID_ARBITRUM, CHAIN_ID_BASE
from wayfinder_paths.core.constants.contracts import (
    ARBITRUM_USDC,
    ARBITRUM_WETH,
    BASE_USDC,
    BASE_WETH,
)
from wayfinder_paths.core.constants.morpho_abi import MORPHO_BLUE_ABI
from wayfinder_paths.core.utils import web3 as web3_utils
from wayfinder_paths.testing.gorlami import gorlami_configured

pytestmark = pytest.mark.skipif(
    not gorlami_configured(),
    reason="api_key not configured (needed for gorlami fork proxy)",
)


@pytest.mark.asyncio
async def test_gorlami_morpho_markets_and_borrow(gorlami):
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
    await gorlami.set_native_balance(fork_info["fork_id"], acct.address, 10**18)
    await gorlami.set_erc20_balance(
        fork_info["fork_id"],
        BASE_USDC,
        acct.address,
        1_000 * 10**6,
    )
    await gorlami.set_erc20_balance(
        fork_info["fork_id"],
        BASE_WETH,
        acct.address,
        1 * 10**18,
    )

    adapter = MorphoAdapter(
        config={"strategy_wallet": {"address": acct.address}},
        strategy_wallet_signing_callback=sign_cb,
    )

    markets = await MORPHO_CLIENT.get_all_markets(chain_id=int(chain_id), listed=True)
    assert markets

    usdc_markets = [
        m
        for m in markets
        if str((m.get("loanAsset") or {}).get("address") or "").lower()
        == BASE_USDC.lower()
    ]
    if not usdc_markets:
        pytest.skip("No USDC loan markets found on Base")

    def _liq(m: dict) -> int:
        try:
            return int((m.get("state") or {}).get("liquidityAssets") or 0)
        except (TypeError, ValueError):
            return 0

    # Pick the deepest USDC market so withdraw-full doesn't fail due to low liquidity.
    lend_market = max(usdc_markets, key=_liq)
    lend_key = str(lend_market["uniqueKey"])

    ok, tx = await adapter.lend(
        chain_id=int(chain_id), market_unique_key=lend_key, qty=10 * 10**6
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    supply_shares, _borrow_shares, _collateral = await adapter._position(
        chain_id=int(chain_id),
        market_unique_key=lend_key,
        account=acct.address,
    )
    assert supply_shares > 0

    # Share-based withdraw simulation should succeed (asset-based full withdraw can revert due to rounding).
    market = await adapter._get_market(chain_id=int(chain_id), unique_key=lend_key)
    market_params = adapter._market_params_from_market(market)
    morpho_addr = await adapter._morpho_address(chain_id=int(chain_id))

    async with web3_utils.web3_from_chain_id(chain_id) as web3:
        morpho = web3.eth.contract(address=morpho_addr, abi=MORPHO_BLUE_ABI)
        assets_withdrawn, shares_withdrawn = await morpho.functions.withdraw(
            market_params,
            0,
            int(supply_shares),
            acct.address,
            acct.address,
        ).call({"from": acct.address}, block_identifier="pending")
        assert int(shares_withdrawn) == int(supply_shares)
        assert int(assets_withdrawn) > 0

    # Borrow flow on a USDC-loan / WETH-collateral market.
    usdc_weth = [
        m
        for m in markets
        if str((m.get("loanAsset") or {}).get("address") or "").lower()
        == BASE_USDC.lower()
        and str((m.get("collateralAsset") or {}).get("address") or "").lower()
        == BASE_WETH.lower()
    ]
    if not usdc_weth:
        pytest.skip("No USDC/WETH market found on Base")

    borrow_market = max(usdc_weth, key=_liq)
    borrow_key = str(borrow_market["uniqueKey"])

    collateral_weth = int(0.05 * 10**18)
    borrow_usdc = 50 * 10**6

    ok, tx = await adapter.supply_collateral(
        chain_id=int(chain_id),
        market_unique_key=borrow_key,
        qty=collateral_weth,
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    _supply_shares, borrow_shares, collateral_assets = await adapter._position(
        chain_id=int(chain_id),
        market_unique_key=borrow_key,
        account=acct.address,
    )
    assert borrow_shares == 0
    assert collateral_assets > 0

    ok, tx = await adapter.borrow(
        chain_id=int(chain_id),
        market_unique_key=borrow_key,
        qty=borrow_usdc,
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    _supply_shares, borrow_shares, _collateral_assets2 = await adapter._position(
        chain_id=int(chain_id),
        market_unique_key=borrow_key,
        account=acct.address,
    )
    assert borrow_shares > 0

    ok, tx = await adapter.repay_full(
        chain_id=int(chain_id),
        market_unique_key=borrow_key,
    )
    assert ok is True, tx
    assert tx is None or (isinstance(tx, str) and tx.startswith("0x"))

    _supply_shares, borrow_shares, _collateral_assets3 = await adapter._position(
        chain_id=int(chain_id),
        market_unique_key=borrow_key,
        account=acct.address,
    )
    assert borrow_shares == 0

    _supply_shares, _borrow_shares, collateral_to_withdraw = await adapter._position(
        chain_id=int(chain_id),
        market_unique_key=borrow_key,
        account=acct.address,
    )
    assert collateral_to_withdraw > 0

    borrow_market_fresh = await adapter._get_market(
        chain_id=int(chain_id), unique_key=borrow_key
    )
    borrow_params = adapter._market_params_from_market(borrow_market_fresh)

    async with web3_utils.web3_from_chain_id(chain_id) as web3:
        morpho = web3.eth.contract(address=morpho_addr, abi=MORPHO_BLUE_ABI)
        # Ensure withdrawCollateral is callable for this position before sending a tx.
        await morpho.functions.withdrawCollateral(
            borrow_params,
            int(collateral_to_withdraw),
            acct.address,
            acct.address,
        ).call({"from": acct.address}, block_identifier="pending")

    ok2, tx2 = await adapter.repay_full(
        chain_id=int(chain_id),
        market_unique_key=borrow_key,
    )
    assert ok2 is True and tx2 is None

    ok, tx = await adapter.withdraw_collateral(
        chain_id=int(chain_id),
        market_unique_key=borrow_key,
        qty=collateral_to_withdraw,
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")


@pytest.mark.asyncio
async def test_gorlami_morpho_bridge_base_to_arbitrum_then_borrow(gorlami):
    base_chain_id = CHAIN_ID_BASE
    arb_chain_id = CHAIN_ID_ARBITRUM

    acct = Account.create()

    async def sign_cb(tx: dict) -> bytes:
        signed = acct.sign_transaction(tx)
        return signed.raw_transaction

    # Trigger fork creation (gorlami fixture patches web3_from_chain_id).
    async with web3_utils.web3_from_chain_id(base_chain_id) as web3:
        assert await web3.eth.chain_id == int(base_chain_id)
    async with web3_utils.web3_from_chain_id(arb_chain_id) as web3:
        assert await web3.eth.chain_id == int(arb_chain_id)

    base_fork = gorlami.forks.get(str(base_chain_id))
    arb_fork = gorlami.forks.get(str(arb_chain_id))
    assert base_fork is not None
    assert arb_fork is not None

    # Fund wallet on Base.
    await gorlami.set_native_balance(base_fork["fork_id"], acct.address, 2 * 10**18)
    await gorlami.set_erc20_balance(
        base_fork["fork_id"], BASE_USDC, acct.address, 1_000 * 10**6
    )

    # Fund wallet on Arbitrum (collateral only; USDC arrives via "bridge" below).
    await gorlami.set_native_balance(arb_fork["fork_id"], acct.address, 2 * 10**18)
    await gorlami.set_erc20_balance(
        arb_fork["fork_id"], ARBITRUM_WETH, acct.address, int(0.25 * 10**18)
    )
    await gorlami.set_erc20_balance(arb_fork["fork_id"], ARBITRUM_USDC, acct.address, 0)

    # Simulate a bridge delivery by moving USDC from Base -> Arbitrum.
    await gorlami.set_erc20_balance(
        base_fork["fork_id"], BASE_USDC, acct.address, 800 * 10**6
    )
    await gorlami.set_erc20_balance(
        arb_fork["fork_id"], ARBITRUM_USDC, acct.address, 200 * 10**6
    )

    adapter = MorphoAdapter(
        config={"strategy_wallet": {"address": acct.address}},
        strategy_wallet_signing_callback=sign_cb,
    )

    markets = await MORPHO_CLIENT.get_all_markets(
        chain_id=int(arb_chain_id), listed=True
    )
    assert markets

    usdc_markets = [
        m
        for m in markets
        if str((m.get("loanAsset") or {}).get("address") or "").lower()
        == ARBITRUM_USDC.lower()
    ]
    if not usdc_markets:
        pytest.skip("No USDC loan markets found on Arbitrum")

    def _liq(m: dict) -> int:
        try:
            return int((m.get("state") or {}).get("liquidityAssets") or 0)
        except (TypeError, ValueError):
            return 0

    lend_market = max(usdc_markets, key=_liq)
    lend_key = str(lend_market["uniqueKey"])

    ok, tx = await adapter.lend(
        chain_id=int(arb_chain_id), market_unique_key=lend_key, qty=10 * 10**6
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    supply_shares, _borrow_shares, _collateral = await adapter._position(
        chain_id=int(arb_chain_id),
        market_unique_key=lend_key,
        account=acct.address,
    )
    assert supply_shares > 0

    # Borrow flow on a USDC-loan / WETH-collateral market.
    usdc_weth = [
        m
        for m in markets
        if str((m.get("loanAsset") or {}).get("address") or "").lower()
        == ARBITRUM_USDC.lower()
        and str((m.get("collateralAsset") or {}).get("address") or "").lower()
        == ARBITRUM_WETH.lower()
    ]
    if not usdc_weth:
        pytest.skip("No USDC/WETH market found on Arbitrum")

    borrow_market = max(usdc_weth, key=_liq)
    borrow_key = str(borrow_market["uniqueKey"])

    collateral_weth = int(0.05 * 10**18)
    borrow_usdc = 50 * 10**6

    ok, tx = await adapter.supply_collateral(
        chain_id=int(arb_chain_id),
        market_unique_key=borrow_key,
        qty=collateral_weth,
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    _supply_shares, borrow_shares, collateral_assets = await adapter._position(
        chain_id=int(arb_chain_id),
        market_unique_key=borrow_key,
        account=acct.address,
    )
    assert borrow_shares == 0
    assert collateral_assets > 0

    ok, tx = await adapter.borrow(
        chain_id=int(arb_chain_id),
        market_unique_key=borrow_key,
        qty=borrow_usdc,
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    _supply_shares, borrow_shares, _collateral_assets2 = await adapter._position(
        chain_id=int(arb_chain_id),
        market_unique_key=borrow_key,
        account=acct.address,
    )
    assert borrow_shares > 0

    ok, tx = await adapter.repay_full(
        chain_id=int(arb_chain_id),
        market_unique_key=borrow_key,
    )
    assert ok is True, tx
    assert tx is None or (isinstance(tx, str) and tx.startswith("0x"))

    _supply_shares, borrow_shares, collateral_to_withdraw = await adapter._position(
        chain_id=int(arb_chain_id),
        market_unique_key=borrow_key,
        account=acct.address,
    )
    assert borrow_shares == 0
    assert collateral_to_withdraw > 0

    ok, tx = await adapter.withdraw_collateral(
        chain_id=int(arb_chain_id),
        market_unique_key=borrow_key,
        qty=collateral_to_withdraw,
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    ok, tx = await adapter.unlend(
        chain_id=int(arb_chain_id),
        market_unique_key=lend_key,
        qty=0,
        withdraw_full=True,
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")
