"""Execution regressions: dev Gorlami forks only, synthetic funds/signers only."""

from copy import deepcopy

import pytest
from eth_abi import decode, encode
from eth_account import Account
from eth_utils import keccak

from wayfinder_paths.adapters.aave_v4_adapter import AaveV4Adapter
from wayfinder_paths.adapters.morpho_adapter import MorphoAdapter
from wayfinder_paths.adapters.uniswap_adapter import UniswapAdapter
from wayfinder_paths.adapters.uniswap_adapter.v4 import best_pool
from wayfinder_paths.core.clients.MorphoClient import MORPHO_CLIENT
from wayfinder_paths.core.config import CONFIG, get_api_key
from wayfinder_paths.core.constants.contracts import UNISWAP_V4_STATE_VIEW
from wayfinder_paths.core.utils import web3 as web3_utils

pytestmark = [pytest.mark.local, pytest.mark.asyncio, pytest.mark.timeout(240)]
CHAIN = 5042
USDC = "0x3600000000000000000000000000000000000000"
EURC = "0xbEf5f6d51CB62b58e6A8f77868681825C6fe21c1"


@pytest.fixture
def dev_api():
    assert get_api_key(), (
        "Set the dev WAYFINDER_TEST_API_KEY CI secret (WAYFINDER_API_KEY locally)"
    )
    previous = deepcopy(CONFIG.get("system", {}))
    CONFIG.setdefault("system", {})["api_base_url"] = (
        "https://strategies-dev.wayfinder.ai/api/v1"
    )
    try:
        yield
    finally:
        CONFIG["system"] = previous


@pytest.fixture
async def fork_wallet(dev_api, gorlami):
    # Force creation BEFORE constructing adapters (some import web3 helpers directly).
    async with web3_utils.web3_from_chain_id(CHAIN) as web3:
        assert await web3.eth.chain_id == CHAIN
    fork = gorlami.forks[str(CHAIN)]
    assert web3_utils._is_gorlami_fork_rpc(fork["rpc_url"])
    account = Account.create()

    async def sign(tx):
        # Last defense: never sign if a later refactor bypasses the fork override.
        assert tx["chainId"] == CHAIN
        assert web3_utils._get_rpcs_for_chain_id(CHAIN) == [fork["rpc_url"]]
        return account.sign_transaction(tx).raw_transaction

    sign.wallet_address = None
    await gorlami.set_native_balance(fork["fork_id"], account.address, 10_000 * 10**18)
    await gorlami.set_erc20_balance(
        fork["fork_id"], EURC, account.address, 1_000 * 10**6
    )
    try:
        yield gorlami, fork["fork_id"], account.address, sign
    finally:
        await MORPHO_CLIENT.aclose()


async def test_arc_morpho_lend_borrow_repay_and_exit(fork_wallet):
    client, fork_id, owner, sign = fork_wallet
    adapter = MorphoAdapter({}, wallet_address=owner, sign_callback=sign)
    markets = await MORPHO_CLIENT.get_all_markets(chain_id=CHAIN, max_pages=2)
    candidates = [
        m
        for m in markets
        if m["loanAsset"]["address"].lower() == USDC.lower()
        and m.get("collateralAsset")
        and int(m["state"]["liquidityAssets"]) > 100 * 10**6
    ]
    assert candidates, "Arc has no liquid USDC collateralized Morpho market"
    market = max(candidates, key=lambda m: int(m["state"]["liquidityAssets"]))
    args = {"chain_id": CHAIN, "market_unique_key": market["marketId"]}
    collateral = market["collateralAsset"]
    collateral_amount = 10 ** int(collateral["decimals"])
    await client.set_erc20_balance(
        fork_id, collateral["address"], owner, collateral_amount
    )
    for method, kwargs in (
        (adapter.lend, {"qty": 100 * 10**6}),
        (adapter.supply_collateral, {"qty": collateral_amount}),
        (adapter.borrow, {"qty": 10**6}),
        (adapter.repay_full, {}),
        (adapter.withdraw_collateral, {"qty": collateral_amount}),
        (adapter.withdraw_full, {}),
    ):
        ok, result = await method(**args, **kwargs)
        assert ok, (method.__name__, result)
    position = await adapter._position(**args, account=owner)
    assert position == (0, 0, 0)


async def test_arc_uniswap_mint_increase_collect_decrease_close(fork_wallet):
    _, _, owner, sign = fork_wallet
    adapter = UniswapAdapter(
        {"chain_id": CHAIN}, wallet_address=owner, sign_callback=sign
    )
    pool = await best_pool(CHAIN, USDC, EURC)
    assert pool is not None, "Arc USDC/EURC pool is unavailable"
    async with web3_utils.web3_from_chain_id(CHAIN) as web3:
        slot = await web3.eth.call(
            {
                "to": UNISWAP_V4_STATE_VIEW[CHAIN],
                "data": keccak(text="getSlot0(bytes32)")[:4]
                + encode(["bytes32"], [bytes.fromhex(pool.key.pool_id[2:])]),
            }
        )
    _, tick, _, _ = decode(["uint160", "int24", "uint24", "uint24"], slot)
    aligned = tick // pool.key.tick_spacing * pool.key.tick_spacing
    limits = {"amount0_max": 100 * 10**6, "amount1_max": 100 * 10**6}
    ok, minted = await adapter.v4_mint_position(
        pool_key=pool.key,
        tick_lower=aligned - 10 * pool.key.tick_spacing,
        tick_upper=aligned + 10 * pool.key.tick_spacing,
        liquidity=10**6,
        **limits,
    )
    assert ok, minted
    assert len(minted["token_ids"]) == 1
    token_id = minted["token_ids"][0]
    for method, kwargs in (
        (adapter.v4_increase_liquidity, {"liquidity": 10**6, **limits}),
        (adapter.v4_collect_fees, {}),
        (
            adapter.v4_decrease_liquidity,
            {"liquidity": 10**6, "amount0_min": 0, "amount1_min": 0},
        ),
    ):
        ok, result = await method(token_id, **kwargs)
        assert ok, (method.__name__, result)
    ok, position = await adapter.v4_get_position(token_id)
    assert ok and position["liquidity"] == 10**6, position
    ok, result = await adapter.v4_close_position(token_id, amount0_min=0, amount1_min=0)
    assert ok, result
    ok, _ = await adapter.v4_get_position(token_id)
    assert not ok, "Burned position must no longer have an owner"


@pytest.mark.parametrize("spoke", ["main", "forex"])
async def test_arc_aave_v4_supply_borrow_repay_exit(fork_wallet, spoke):
    _, _, owner, sign = fork_wallet
    adapter = AaveV4Adapter(wallet_address=owner, sign_callback=sign)
    ok, reserves = await adapter.get_markets(spoke=spoke)
    assert ok, reserves
    usdc = next(
        r["reserve_id"] for r in reserves if r["underlying"].lower() == USDC.lower()
    )
    eurc = next(
        r["reserve_id"] for r in reserves if r["underlying"].lower() == EURC.lower()
    )
    for method, args in (
        (adapter.supply, (usdc, 500 * 10**6)),
        (adapter.set_collateral, (usdc, True)),
        (adapter.borrow, (eurc, 10**6)),
        (adapter.repay, (eurc, 2 * 10**6)),
        (adapter.withdraw, (usdc, 500 * 10**6)),
    ):
        ok, result = await method(*args, spoke=spoke)
        assert ok, (method.__name__, result)
    ok, state = await adapter.get_user_state(spoke=spoke)
    assert ok, state
    assert all(p["debt"] == 0 for p in state["positions"])
