from __future__ import annotations

import pytest
from eth_account import Account

from wayfinder_paths.adapters.ethena_vault_adapter.adapter import EthenaVaultAdapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_ETHEREUM
from wayfinder_paths.core.constants.ethena_abi import ETHENA_SUSDE_VAULT_ABI
from wayfinder_paths.core.constants.ethena_contracts import (
    ETHENA_SUSDE_VAULT_MAINNET,
    ETHENA_USDE_MAINNET,
)
from wayfinder_paths.core.utils import web3 as web3_utils
from wayfinder_paths.core.utils.tokens import get_token_balance, get_token_decimals
from wayfinder_paths.testing.gorlami import gorlami_configured

pytestmark = pytest.mark.skipif(
    not gorlami_configured(),
    reason="api_key not configured (needed for gorlami fork proxy)",
)


async def _try_time_travel(gorlami, fork_id: str, *, target_ts: int) -> bool:
    # Try common dev-node time travel methods (anvil/hardhat).
    candidates = [
        ("evm_setNextBlockTimestamp", [target_ts]),
        ("anvil_setNextBlockTimestamp", [target_ts]),
        ("hardhat_setNextBlockTimestamp", [target_ts]),
    ]
    for method, params in candidates:
        try:
            await gorlami.send_rpc(fork_id, method, params)
            await gorlami.send_rpc(fork_id, "evm_mine", [])
            return True
        except Exception:
            continue

    # Fallback: increase time by delta if setNextBlockTimestamp isn't supported.
    try:
        block = await gorlami.send_rpc(
            fork_id, "eth_getBlockByNumber", ["latest", False]
        )
        now = int(block.get("timestamp") or "0x0", 16) if isinstance(block, dict) else 0
    except Exception:
        now = 0

    delta = max(0, int(target_ts) - int(now))
    if delta <= 0:
        return True

    for method in ("evm_increaseTime", "anvil_increaseTime", "hardhat_increaseTime"):
        try:
            await gorlami.send_rpc(fork_id, method, [delta])
            await gorlami.send_rpc(fork_id, "evm_mine", [])
            return True
        except Exception:
            continue
    return False


@pytest.mark.asyncio
async def test_gorlami_ethena_deposit_cooldown_and_claim(gorlami):
    chain_id = CHAIN_ID_ETHEREUM

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
    await gorlami.set_native_balance(fork_info["fork_id"], acct.address, 10**19)

    # Determine USDe decimals for deposit sizing.
    async with web3_utils.web3_from_chain_id(chain_id) as web3:
        decimals = await get_token_decimals(
            ETHENA_USDE_MAINNET, chain_id, web3=web3, block_identifier="latest"
        )
        decimals = int(decimals)

    deposit_amount = 100 * 10**decimals
    await gorlami.set_erc20_balance(
        fork_info["fork_id"],
        ETHENA_USDE_MAINNET,
        acct.address,
        deposit_amount,
    )

    adapter = EthenaVaultAdapter(
        config={},
        sign_callback=sign_cb,
        wallet_address=acct.address,
    )

    ok, tx = await adapter.deposit_usde(amount_assets=deposit_amount)
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    async with web3_utils.web3_from_chain_id(chain_id) as web3:
        shares = await get_token_balance(
            ETHENA_SUSDE_VAULT_MAINNET,
            chain_id,
            acct.address,
            web3=web3,
            block_identifier="pending",
        )
        shares = int(shares)
    assert shares > 0

    ok, state = await adapter.get_full_user_state(
        account=acct.address,
        chain_id=chain_id,
        include_apy=True,
        include_zero_positions=True,
    )
    assert ok is True, state
    assert isinstance(state, dict)
    assert state.get("protocol") == "ethena"

    # Start cooldown by burning shares.
    ok, tx2 = await adapter.request_withdraw_by_shares(shares=shares)
    assert ok is True, tx2
    assert isinstance(tx2, str) and tx2.startswith("0x")

    async with web3_utils.web3_from_chain_id(chain_id) as web3:
        vault = web3.eth.contract(
            address=ETHENA_SUSDE_VAULT_MAINNET, abi=ETHENA_SUSDE_VAULT_ABI
        )
        cooldown_end, underlying_amount = await vault.functions.cooldowns(
            acct.address
        ).call(block_identifier="pending")
        cooldown_end_i = int(cooldown_end or 0)
        underlying_i = int(underlying_amount or 0)
    assert cooldown_end_i > 0
    assert underlying_i > 0

    # Claim should fail before cooldown is finished (adapter should pre-check).
    ok, msg = await adapter.claim_withdraw(require_matured=True)
    assert ok is False

    # Fast-forward time and claim.
    advanced = await _try_time_travel(
        gorlami, fork_info["fork_id"], target_ts=cooldown_end_i + 1
    )
    assert advanced is True

    ok, tx3 = await adapter.claim_withdraw(require_matured=True)
    assert ok is True, tx3
    assert isinstance(tx3, str) and tx3.startswith("0x")
