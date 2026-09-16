"""Cross-chain execution regressions: dev forks and synthetic funds only."""

from collections.abc import AsyncIterator, Awaitable, Callable
from functools import wraps
from typing import Any

import pytest
from eth_account import Account

from wayfinder_paths.adapters.morpho_adapter import MorphoAdapter
from wayfinder_paths.adapters.uniswap_adapter import UniswapAdapter
from wayfinder_paths.core.clients.GorlamiTestnetClient import GorlamiTestnetClient
from wayfinder_paths.core.clients.MorphoClient import MORPHO_CLIENT
from wayfinder_paths.core.config import CONFIG, get_api_key
from wayfinder_paths.core.constants.contracts import BASE_USDC, ZERO_ADDRESS
from wayfinder_paths.core.constants.morpho_contracts import MORPHO_BY_CHAIN
from wayfinder_paths.core.utils import web3 as web3_utils
from wayfinder_paths.core.utils.gorlami import gorlami_fork
from wayfinder_paths.core.utils.tokens import get_token_balance
from wayfinder_paths.core.utils.wallets import get_local_sign_callback

pytestmark = [pytest.mark.local, pytest.mark.asyncio, pytest.mark.timeout(240)]
USDC = {1: "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 8453: BASE_USDC}
INDEX = "0x56910d4409f3a0c78c64dd8d0545ff0705389870"
type ForkWallet = tuple[
    GorlamiTestnetClient, str, str, Callable[[dict[str, Any]], Awaitable[bytes]]
]


@pytest.fixture
async def fork_wallet(
    chain_id: int, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[ForkWallet]:
    assert get_api_key(), "A dev WAYFINDER_API_KEY is required for fork execution"
    monkeypatch.setitem(
        CONFIG,
        "system",
        {
            **CONFIG.get("system", {}),
            "api_base_url": "https://strategies-dev.wayfinder.ai/api/v1",
        },
    )
    account = Account.create()
    local_sign = get_local_sign_callback(account.key.hex())
    async with gorlami_fork(chain_id, native_balances={account.address: 10**18}) as (
        client,
        fork,
    ):
        assert web3_utils._is_gorlami_fork_rpc(fork["rpc_url"])
        async with web3_utils.web3_from_chain_id(chain_id) as web3:
            assert await web3.eth.chain_id == chain_id

        @wraps(local_sign)
        async def sign(tx: dict[str, Any]) -> bytes:
            assert tx["chainId"] == chain_id
            assert web3_utils._get_rpcs_for_chain_id(chain_id) == [fork["rpc_url"]]
            return await local_sign(tx)

        try:
            yield client, fork["fork_id"], account.address, sign
        finally:
            await MORPHO_CLIENT.aclose()


@pytest.mark.parametrize("chain_id", [1, 8453, 4663])
async def test_uniswap_native_and_erc20_swap_round_trip(
    chain_id: int, fork_wallet: ForkWallet
) -> None:
    _, _, owner, sign = fork_wallet
    token = INDEX if chain_id == 4663 else USDC[chain_id]
    adapter = UniswapAdapter(
        {"chain_id": chain_id}, wallet_address=owner, sign_callback=sign
    )
    for token_in, token_out, amount in (
        (ZERO_ADDRESS, token, 10**15),
        (token, ZERO_ADDRESS, None),
    ):
        if amount is None:
            amount = await get_token_balance(token, chain_id, owner)
        before = await get_token_balance(token_out, chain_id, owner)
        ok, result = await adapter.v4_swap_exact_in(
            token_in=token_in, token_out=token_out, amount_in=amount
        )
        assert ok, result
        async with web3_utils.web3_from_chain_id(chain_id) as web3:
            receipt = await web3.eth.get_transaction_receipt(result["tx_hash"])
            assert receipt["status"] == 1
        after = await get_token_balance(token_out, chain_id, owner)
        if token_out == ZERO_ADDRESS:
            # Native balance also pays for both Permit2 approval transactions.
            assert after > before
        else:
            assert after - before >= result["min_out"] > 0
    assert await get_token_balance(token, chain_id, owner) == 0


@pytest.mark.parametrize("chain_id", [1, 8453])
async def test_morpho_market_and_vault_round_trips(
    chain_id: int, fork_wallet: ForkWallet
) -> None:
    client, fork_id, owner, sign = fork_wallet
    adapter = MorphoAdapter({}, wallet_address=owner, sign_callback=sign)
    deployments = await MORPHO_CLIENT.get_morpho_by_chain()
    for field in ("morpho", "public_allocator"):
        assert (
            deployments[chain_id][field].lower()
            == MORPHO_BY_CHAIN[chain_id][field].lower()
        )
    markets = await MORPHO_CLIENT.get_all_markets(chain_id=chain_id, max_pages=2)
    market = max(
        (
            m
            for m in markets
            if m["loanAsset"]["address"].lower() == USDC[chain_id].lower()
            and m.get("collateralAsset")
            and m.get("state")
        ),
        key=lambda m: int(m["state"]["liquidityAssets"]),
    )
    collateral = market["collateralAsset"]
    collateral_amount = 10 ** int(collateral["decimals"])
    await client.set_erc20_balance(fork_id, USDC[chain_id], owner, 1000 * 10**6)
    await client.set_erc20_balance(
        fork_id, collateral["address"], owner, collateral_amount
    )
    args = {"chain_id": chain_id, "market_unique_key": market["marketId"]}
    for method, kwargs in (
        (adapter.lend, {"qty": 10 * 10**6}),
        (adapter.supply_collateral, {"qty": collateral_amount}),
        (adapter.borrow, {"qty": 10**6}),
        (adapter.repay_full, {}),
        (adapter.withdraw_collateral, {"qty": collateral_amount}),
        (adapter.withdraw_full, {}),
    ):
        ok, tx = await method(**args, **kwargs)
        assert ok, (method.__name__, tx)
        async with web3_utils.web3_from_chain_id(chain_id) as web3:
            assert (await web3.eth.get_transaction_receipt(tx))["status"] == 1
    assert await adapter._position(**args, account=owner) == (0, 0, 0)

    vaults = await MORPHO_CLIENT.get_all_vaults(chain_id=chain_id, max_pages=1)
    vault = max(
        (
            v
            for v in vaults
            if v["asset"]["address"].lower() == USDC[chain_id].lower()
            and v.get("state")
        ),
        key=lambda v: int(v["state"]["totalAssets"]),
    )["address"]
    ok, tx = await adapter.vault_deposit(
        chain_id=chain_id, vault_address=vault, assets=10 * 10**6
    )
    assert ok, tx
    shares = await get_token_balance(vault, chain_id, owner)
    assert shares > 0
    before = await get_token_balance(USDC[chain_id], chain_id, owner)
    ok, tx = await adapter.vault_redeem(
        chain_id=chain_id, vault_address=vault, shares=shares
    )
    assert ok, tx
    assert await get_token_balance(vault, chain_id, owner) == 0
    assert await get_token_balance(USDC[chain_id], chain_id, owner) > before
