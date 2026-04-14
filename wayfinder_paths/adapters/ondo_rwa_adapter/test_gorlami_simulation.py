from __future__ import annotations

import pytest
from eth_account import Account

from wayfinder_paths.adapters.ondo_rwa_adapter.adapter import OndoRwaAdapter
from wayfinder_paths.core.constants.ondo_rwa_contracts import ONDO_RWA_MARKETS
from wayfinder_paths.core.utils import web3 as web3_utils
from wayfinder_paths.core.utils.tokens import get_token_balance
from wayfinder_paths.testing.gorlami import gorlami_configured

pytestmark = pytest.mark.skipif(
    not gorlami_configured(),
    reason="api_key not configured (needed for gorlami fork proxy)",
)

CHAIN_ID_ETH = 1
CHAIN_ID_MANTLE = 5000
ETH_USDC = ONDO_RWA_MARKETS[("ousg", CHAIN_ID_ETH)]["stablecoins"]["usdc"]["address"]
ETH_USDY = ONDO_RWA_MARKETS[("usdy", CHAIN_ID_ETH)]["token"]
RUSDY = ONDO_RWA_MARKETS[("rusdy", CHAIN_ID_ETH)]["token"]
MANTLE_USDY = ONDO_RWA_MARKETS[("usdy", CHAIN_ID_MANTLE)]["token"]
MUSD = ONDO_RWA_MARKETS[("musd", CHAIN_ID_MANTLE)]["token"]


def _make_adapter(acct) -> OndoRwaAdapter:
    async def sign_cb(tx: dict) -> bytes:
        signed = acct.sign_transaction(tx)
        return signed.raw_transaction

    return OndoRwaAdapter(
        config={},
        sign_callback=sign_cb,
        wallet_address=acct.address,
    )


async def _ensure_fork(gorlami, chain_id: int) -> str:
    try:
        async with web3_utils.web3_from_chain_id(chain_id) as web3:
            assert await web3.eth.chain_id == int(chain_id)
    except Exception as exc:  # noqa: BLE001 - skip when Gorlami cannot fork the chain
        pytest.skip(f"gorlami could not create fork for chain_id={chain_id}: {exc}")

    fork_info = gorlami.forks.get(str(chain_id))
    assert fork_info is not None
    return fork_info["fork_id"]


async def _fund_wallet(
    gorlami,
    *,
    fork_id: str,
    account,
    native_wei: int,
    erc20_balances: dict[str, int] | None = None,
) -> None:
    await gorlami.set_native_balance(fork_id, account.address, native_wei)
    for token, amount in (erc20_balances or {}).items():
        await gorlami.set_erc20_balance(fork_id, token, account.address, amount)


@pytest.mark.asyncio
async def test_gorlami_eth_allowlist_and_registry_reads(gorlami):
    await _ensure_fork(gorlami, CHAIN_ID_ETH)

    adapter = OndoRwaAdapter(config={})

    ok, usdc_supported = await adapter.is_subscription_token_supported(
        product_family="ousg",
        token=ETH_USDC,
        chain_id=CHAIN_ID_ETH,
    )
    assert ok is True
    assert usdc_supported is True

    ok, registered = await adapter.is_registered_or_eligible(
        account=Account.create().address,
        product_family="ousg",
    )
    assert ok is True
    assert isinstance(registered, dict)
    assert registered["eligible"] is False


@pytest.mark.asyncio
async def test_gorlami_get_all_markets_and_allowlist_reads(gorlami):
    await _ensure_fork(gorlami, CHAIN_ID_ETH)
    await _ensure_fork(gorlami, CHAIN_ID_MANTLE)

    adapter = OndoRwaAdapter(config={})

    ok, markets = await adapter.get_all_markets()
    assert ok is True, markets

    keys = {(market["product"], int(market["chain_id"])) for market in markets}
    assert ("ousg", 1) in keys
    assert ("rousg", 1) in keys
    assert ("usdy", 1) in keys
    assert ("rusdy", 1) in keys
    assert ("usdy", 5000) in keys
    assert ("musd", 5000) in keys
    assert ("ousg", 137) in keys
    assert ("usdy", 42161) in keys

    ok, usdc_supported = await adapter.is_subscription_token_supported(
        product_family="ousg",
        token=ETH_USDC,
        chain_id=CHAIN_ID_ETH,
    )
    assert ok is True
    assert usdc_supported is True


@pytest.mark.asyncio
async def test_gorlami_rusdy_wrap_unwrap_round_trip(gorlami):
    fork_id = await _ensure_fork(gorlami, CHAIN_ID_ETH)

    acct = Account.create()
    adapter = _make_adapter(acct)
    await _fund_wallet(
        gorlami,
        fork_id=fork_id,
        account=acct,
        native_wei=2 * 10**18,
        erc20_balances={ETH_USDY: 20 * 10**18},
    )

    ok, wrap_tx = await adapter.wrap(
        product="usdy",
        chain_id=CHAIN_ID_ETH,
        amount=5 * 10**18,
    )
    assert ok is True, wrap_tx
    assert isinstance(wrap_tx, str) and wrap_tx.startswith("0x")

    async with web3_utils.web3_from_chain_id(CHAIN_ID_ETH) as web3:
        rusdy_balance = int(
            await get_token_balance(
                RUSDY,
                CHAIN_ID_ETH,
                acct.address,
                web3=web3,
                block_identifier="pending",
            )
        )
    assert rusdy_balance > 0

    ok, unwrap_tx = await adapter.unwrap(
        product="rusdy",
        chain_id=CHAIN_ID_ETH,
        amount=rusdy_balance,
    )
    assert ok is True, unwrap_tx
    assert isinstance(unwrap_tx, str) and unwrap_tx.startswith("0x")

    async with web3_utils.web3_from_chain_id(CHAIN_ID_ETH) as web3:
        usdy_after = int(
            await get_token_balance(
                ETH_USDY,
                CHAIN_ID_ETH,
                acct.address,
                web3=web3,
                block_identifier="pending",
            )
        )
        rusdy_after = int(
            await get_token_balance(
                RUSDY,
                CHAIN_ID_ETH,
                acct.address,
                web3=web3,
                block_identifier="pending",
            )
        )
    assert usdy_after > 0
    assert rusdy_after == 0


@pytest.mark.asyncio
async def test_gorlami_musd_wrap_unwrap_round_trip(gorlami):
    fork_id = await _ensure_fork(gorlami, CHAIN_ID_MANTLE)

    acct = Account.create()
    adapter = _make_adapter(acct)
    await _fund_wallet(
        gorlami,
        fork_id=fork_id,
        account=acct,
        native_wei=2 * 10**18,
        erc20_balances={MANTLE_USDY: 20 * 10**18},
    )

    ok, wrap_tx = await adapter.wrap(
        product="usdy",
        chain_id=CHAIN_ID_MANTLE,
        amount=5 * 10**18,
    )
    assert ok is True, wrap_tx
    assert isinstance(wrap_tx, str) and wrap_tx.startswith("0x")

    async with web3_utils.web3_from_chain_id(CHAIN_ID_MANTLE) as web3:
        musd_balance = int(
            await get_token_balance(
                MUSD,
                CHAIN_ID_MANTLE,
                acct.address,
                web3=web3,
                block_identifier="pending",
            )
        )
    assert musd_balance > 0

    ok, unwrap_tx = await adapter.unwrap(
        product="musd",
        chain_id=CHAIN_ID_MANTLE,
        amount=musd_balance,
    )
    assert ok is True, unwrap_tx
    assert isinstance(unwrap_tx, str) and unwrap_tx.startswith("0x")

    async with web3_utils.web3_from_chain_id(CHAIN_ID_MANTLE) as web3:
        usdy_after = int(
            await get_token_balance(
                MANTLE_USDY,
                CHAIN_ID_MANTLE,
                acct.address,
                web3=web3,
                block_identifier="pending",
            )
        )
        musd_after = int(
            await get_token_balance(
                MUSD,
                CHAIN_ID_MANTLE,
                acct.address,
                web3=web3,
                block_identifier="pending",
            )
        )
    assert usdy_after > 0
    assert musd_after == 0


@pytest.mark.asyncio
async def test_gorlami_get_pos_and_full_state_after_musd_wrap(gorlami):
    fork_id = await _ensure_fork(gorlami, CHAIN_ID_MANTLE)

    acct = Account.create()
    adapter = _make_adapter(acct)
    await _fund_wallet(
        gorlami,
        fork_id=fork_id,
        account=acct,
        native_wei=2 * 10**18,
        erc20_balances={MANTLE_USDY: 15 * 10**18},
    )

    ok, wrap_tx = await adapter.wrap(
        product="usdy",
        chain_id=CHAIN_ID_MANTLE,
        amount=5 * 10**18,
    )
    assert ok is True, wrap_tx

    ok, pos = await adapter.get_pos(
        account=acct.address,
        chain_id=CHAIN_ID_MANTLE,
        include_usd=True,
        include_zero_positions=True,
    )
    assert ok is True, pos
    products = {entry["product"] for entry in pos["positions"]}
    assert {"usdy", "musd"} <= products

    musd_pos = next(entry for entry in pos["positions"] if entry["product"] == "musd")
    assert musd_pos["balance_raw"] > 0
    assert musd_pos["underlying_equivalent_raw"] > 0

    ok, state = await adapter.get_full_user_state(
        account=acct.address,
        include_usd=True,
        include_zero_positions=True,
    )
    assert ok is True, state
    assert state["protocol"] == "ondo_rwa"
    assert "musd" in state["positions_by_product"]


@pytest.mark.asyncio
async def test_gorlami_ousg_subscribe_rejects_noncompliant_wallet(gorlami):
    fork_id = await _ensure_fork(gorlami, CHAIN_ID_ETH)

    acct = Account.create()
    adapter = _make_adapter(acct)
    await _fund_wallet(
        gorlami,
        fork_id=fork_id,
        account=acct,
        native_wei=2 * 10**18,
        erc20_balances={ETH_USDC: 10_000 * 10**6},
    )

    ok, result = await adapter.subscribe(
        product="ousg",
        chain_id=CHAIN_ID_ETH,
        deposit_token=ETH_USDC,
        amount=6_000 * 10**6,
        min_received=1,
    )
    assert ok is False
    assert isinstance(result, str) and result
