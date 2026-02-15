from __future__ import annotations

import pytest
from eth_account import Account

from wayfinder_paths.adapters.morpho_adapter.adapter import MorphoAdapter
from wayfinder_paths.core.clients.MorphoClient import MORPHO_CLIENT
from wayfinder_paths.core.config import get_api_key
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE
from wayfinder_paths.core.constants.morpho_abi import MORPHO_BLUE_ABI
from wayfinder_paths.core.utils import web3 as web3_utils


def gorlami_configured() -> bool:
    return bool(get_api_key())


pytestmark = pytest.mark.skipif(
    not gorlami_configured(),
    reason="api_key not configured (needed for gorlami fork proxy)",
)


BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BASE_WETH = "0x4200000000000000000000000000000000000006"


@pytest.mark.asyncio
async def test_gorlami_morpho_markets_and_vaults(gorlami):
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
    assert ok is True and isinstance(tx, str) and tx.startswith("0x")

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
