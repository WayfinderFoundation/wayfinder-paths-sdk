#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
import math
import time
from pathlib import Path
from typing import Any

from eth_account import Account
from eth_utils import to_checksum_address
from loguru import logger

from wayfinder_paths.core.constants.contracts import (
    BASE_USDC,
    PERMIT2,
    UNISWAP_V4_POSITION_MANAGER,
    UNISWAP_V4_STATE_VIEW,
)
from wayfinder_paths.core.utils.contracts import deploy_contract
from wayfinder_paths.core.utils.gorlami import gorlami_fork
from wayfinder_paths.core.utils.tokens import (
    ensure_allowance,
    get_token_balance,
    get_token_decimals,
)
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction
from wayfinder_paths.core.utils.transaction import wait_for_transaction_receipt
from wayfinder_paths.core.utils.uniswap_v3_math import (
    liq_for_amt0,
    liq_for_amt1,
    liq_for_amounts,
    price_to_sqrt_price_x96,
    sqrt_price_x96_from_tick,
    tick_from_sqrt_price_x96,
)
from wayfinder_paths.core.utils.uniswap_v4_deploy import (
    build_pool_key,
    get_slot0,
    posm_initialize_and_mint,
    pool_id,
    sort_currencies,
)
from wayfinder_paths.core.utils.web3 import web3_from_chain_id
from wayfinder_paths.mcp.utils import find_wallet_by_label

CHAIN_ID_BASE = 8453
PROMPT_BASE = to_checksum_address("0x30c7235866872213f68cb1f08c37cb9eccb93452")
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
MIN_TICK = -887272
MAX_TICK = 887272


def _raw(amount: str | int | Decimal, decimals: int) -> int:
    return int(Decimal(str(amount)) * (Decimal(10) ** int(decimals)))


def _human(amount_raw: int, decimals: int) -> Decimal:
    return Decimal(int(amount_raw)) / (Decimal(10) ** int(decimals))


def _make_sign_callback(private_key: str):
    account = Account.from_key(private_key)

    async def sign_callback(transaction: dict) -> bytes:
        signed = account.sign_transaction(transaction)
        return signed.raw_transaction

    return sign_callback


def _repo_root() -> Path:
    cur = Path(__file__).resolve()
    for parent in [cur, *cur.parents]:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


def _read_sol(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _sqrt_price_for_pair(
    *,
    token_a: str,
    token_b: str,
    price_b_per_a: Decimal,
    decimals_a: int,
    decimals_b: int,
) -> int:
    currency0, currency1 = sort_currencies(token_a, token_b)
    if currency0.lower() == to_checksum_address(token_a).lower():
        price_1_per_0 = float(price_b_per_a)
        dec0, dec1 = decimals_a, decimals_b
    else:
        price_1_per_0 = float(Decimal(1) / price_b_per_a)
        dec0, dec1 = decimals_b, decimals_a
    return price_to_sqrt_price_x96(price_1_per_0, dec0, dec1)


def _pinned_block(receipt: Any) -> int | None:
    if not isinstance(receipt, Mapping):
        return None
    block_number = receipt.get("blockNumber")
    if block_number is None:
        return None
    try:
        if isinstance(block_number, str) and block_number.startswith("0x"):
            return int(block_number, 16)
        return int(block_number)
    except (TypeError, ValueError):
        return None


async def _balance_after_tx(
    *,
    token_address: str | None,
    wallet_address: str,
    pinned_block_number: int | None,
    min_expected: int = 1,
    attempts: int = 5,
) -> int:
    bal = 0
    block_id: str | int = (
        int(pinned_block_number) if pinned_block_number is not None else "pending"
    )

    for i in range(int(attempts)):
        try:
            bal = await get_token_balance(
                token_address, CHAIN_ID_BASE, wallet_address, block_identifier=block_id
            )
        except Exception as exc:
            logger.warning(
                f"Balance read failed for {token_address} at block {block_id}: {exc}"
            )
            bal = 0

        if bal >= int(min_expected):
            return int(bal)
        await asyncio.sleep(1 + i)

    if pinned_block_number is None:
        return int(bal)

    # Fallback (un-pinned).
    for i in range(int(attempts)):
        try:
            bal = await get_token_balance(
                token_address, CHAIN_ID_BASE, wallet_address, block_identifier="pending"
            )
        except Exception as exc:
            logger.warning(
                f"Balance read failed for {token_address} at pending: {exc}"
            )
            bal = 0

        if bal >= int(min_expected):
            return int(bal)
        await asyncio.sleep(1 + i)

    return int(bal)


def _full_range_ticks(tick_spacing: int) -> tuple[int, int]:
    # Mirror Solidity's int24 division behavior (truncate toward 0).
    lower = math.trunc(MIN_TICK / tick_spacing) * tick_spacing
    upper = math.trunc(MAX_TICK / tick_spacing) * tick_spacing
    return int(lower), int(upper)

def _floor_to_spacing(tick: int, spacing: int) -> int:
    if spacing <= 0:
        return int(tick)
    return int((int(tick) // int(spacing)) * int(spacing))


def _ceil_to_spacing(tick: int, spacing: int) -> int:
    if spacing <= 0:
        return int(tick)
    # ceil(tick/spacing) * spacing
    return int((-(-int(tick) // int(spacing))) * int(spacing))


def _concentrated_ticks(
    *, sqrt_price_x96: int, tick_spacing: int, width_ticks: int
) -> tuple[int, int]:
    if width_ticks <= 0:
        raise ValueError("width_ticks must be > 0")

    current_tick = int(tick_from_sqrt_price_x96(float(sqrt_price_x96)))

    tick_lower = _floor_to_spacing(
        current_tick - int(width_ticks), int(tick_spacing)
    )
    tick_upper = _ceil_to_spacing(
        current_tick + int(width_ticks), int(tick_spacing)
    )

    min_tick, max_tick = _full_range_ticks(int(tick_spacing))
    tick_lower = max(int(tick_lower), int(min_tick))
    tick_upper = min(int(tick_upper), int(max_tick))

    if tick_lower >= tick_upper:
        tick_lower = max(
            int(min_tick),
            _floor_to_spacing(current_tick - int(tick_spacing), int(tick_spacing)),
        )
        tick_upper = min(
            int(max_tick),
            _ceil_to_spacing(current_tick + int(tick_spacing), int(tick_spacing)),
        )

    if tick_lower >= tick_upper:
        raise ValueError(
            f"Invalid tick range: lower={tick_lower} upper={tick_upper} spacing={tick_spacing}"
        )

    return int(tick_lower), int(tick_upper)


def _liq_for_full_range(
    *, sqrt_price_x96: int, tick_lower: int, tick_upper: int, amount0: int, amount1: int
) -> int:
    sqrt_a = sqrt_price_x96_from_tick(int(tick_lower))
    sqrt_b = sqrt_price_x96_from_tick(int(tick_upper))
    return int(liq_for_amounts(int(sqrt_price_x96), int(sqrt_a), int(sqrt_b), int(amount0), int(amount1)))


@dataclass(frozen=True)
class DeployedContract:
    address: str
    abi: list[dict[str, Any]]


async def _latest_timestamp(chain_id: int) -> int:
    async with web3_from_chain_id(int(chain_id)) as w3:
        blk = await w3.eth.get_block("latest")
        ts = blk.get("timestamp") if isinstance(blk, Mapping) else None
        try:
            return int(ts) if ts is not None else int(time.time())
        except (TypeError, ValueError):
            return int(time.time())


async def _deploy_from_file(
    *,
    rel_path: str,
    contract_name: str,
    constructor_args: list[Any],
    from_address: str,
    chain_id: int,
    sign_callback,
) -> DeployedContract:
    root = _repo_root()
    src_path = root / rel_path
    source_code = _read_sol(src_path)
    result = await deploy_contract(
        source_code=source_code,
        contract_name=contract_name,
        source_filename=rel_path.replace("\\", "/"),
        constructor_args=constructor_args,
        from_address=from_address,
        chain_id=chain_id,
        sign_callback=sign_callback,
        verify=False,
        escape_hatch=False,
        project_root=str(root),
    )
    return DeployedContract(
        address=to_checksum_address(result["contract_address"]),
        abi=result["abi"],
    )


async def _run_flow(
    *,
    wallet_address: str,
    sign_callback,
    fee: int,
    ap_fee: int,
    deposit_usdc: Decimal,
    seed_usdc_vu: Decimal,
    seed_prompt: Decimal,
    seed_agent: Decimal,
    seed_ap_agent: Decimal,
    av_width_ticks: int,
    ap_width_ticks: int,
    ap_lock_forever: bool,
    locker_unlock_days: int,
    locker_fee_share_bps: int,
    locker_early_exit_bps: int,
    locker_beneficiary: str,
    price_v_per_prompt: Decimal,
    price_v_per_agent: Decimal,
    price_p_per_agent: Decimal,
    price_usdc_per_v: Decimal,
) -> int:
    posm = UNISWAP_V4_POSITION_MANAGER[CHAIN_ID_BASE]
    state_view = UNISWAP_V4_STATE_VIEW[CHAIN_ID_BASE]
    permit2 = PERMIT2[CHAIN_ID_BASE]

    async with web3_from_chain_id(CHAIN_ID_BASE) as w3:
        code = await w3.eth.get_code(PROMPT_BASE)
        if not code:
            raise RuntimeError(f"PROMPT contract not found on Base: {PROMPT_BASE}")

    prompt_decimals = await get_token_decimals(PROMPT_BASE, CHAIN_ID_BASE)
    usdc_decimals = await get_token_decimals(BASE_USDC, CHAIN_ID_BASE)
    logger.info(f"PROMPT decimals={prompt_decimals} USDC decimals={usdc_decimals}")

    fee_vault = await _deploy_from_file(
        rel_path="contracts/ecosystem/FeeVault.sol",
        contract_name="FeeVault",
        constructor_args=[wallet_address],
        from_address=wallet_address,
        chain_id=CHAIN_ID_BASE,
        sign_callback=sign_callback,
    )
    logger.info(f"FeeVault: {fee_vault.address}")

    vault = await _deploy_from_file(
        rel_path="contracts/ecosystem/WayfinderVault4626.sol",
        contract_name="WayfinderVault4626",
        constructor_args=[
            BASE_USDC,
            "Wayfinder Vault Share",
            "wfV",
            wallet_address,
        ],
        from_address=wallet_address,
        chain_id=CHAIN_ID_BASE,
        sign_callback=sign_callback,
    )
    logger.info(f"Vault (V): {vault.address}")

    v_decimals = await get_token_decimals(vault.address, CHAIN_ID_BASE)
    logger.info(f"V decimals={v_decimals}")

    factory_ct = await _deploy_from_file(
        rel_path="contracts/agents/AgentTokenFactory.sol",
        contract_name="AgentTokenFactory",
        constructor_args=[wallet_address],
        from_address=wallet_address,
        chain_id=CHAIN_ID_BASE,
        sign_callback=sign_callback,
    )
    logger.info(f"AgentTokenFactory: {factory_ct.address}")

    agent_supply = _raw("1000000", 18)
    create_tx = await encode_call(
        target=factory_ct.address,
        abi=factory_ct.abi,
        fn_name="createAgentToken",
        args=[
            "Agent One",
            "AG1",
            wallet_address,
            int(agent_supply),
            wallet_address,
        ],
        from_address=wallet_address,
        chain_id=CHAIN_ID_BASE,
    )
    create_hash = await send_transaction(create_tx, sign_callback, wait_for_receipt=True)
    receipt = await wait_for_transaction_receipt(
        CHAIN_ID_BASE, create_hash, confirmations=0
    )
    async with web3_from_chain_id(CHAIN_ID_BASE) as w3:
        factory_contract = w3.eth.contract(
            address=factory_ct.address, abi=factory_ct.abi
        )
        evs = factory_contract.events.AgentTokenCreated().process_receipt(receipt)
    if not evs:
        raise RuntimeError("AgentTokenCreated event not found in receipt")
    agent_token = to_checksum_address(evs[0]["args"]["token"])

    logger.info(f"Agent token (A): {agent_token}")

    locker = await _deploy_from_file(
        rel_path="contracts/ecosystem/LiquidityLockerV4.sol",
        contract_name="LiquidityLockerV4",
        constructor_args=[wallet_address, posm, fee_vault.address],
        from_address=wallet_address,
        chain_id=CHAIN_ID_BASE,
        sign_callback=sign_callback,
    )
    logger.info(f"LiquidityLockerV4: {locker.address}")

    pinned: int | None = None
    if deposit_usdc > 0:
        # Mint V shares by depositing USDC into the vault.
        deposit_usdc_raw = _raw(deposit_usdc, usdc_decimals)
        await ensure_allowance(
            token_address=BASE_USDC,
            owner=wallet_address,
            spender=vault.address,
            amount=int(deposit_usdc_raw),
            chain_id=CHAIN_ID_BASE,
            signing_callback=sign_callback,
            approval_amount=int(deposit_usdc_raw),
        )
        deposit_tx = await encode_call(
            target=vault.address,
            abi=vault.abi,
            fn_name="deposit",
            args=[int(deposit_usdc_raw), wallet_address],
            from_address=wallet_address,
            chain_id=CHAIN_ID_BASE,
        )
        deposit_hash = await send_transaction(
            deposit_tx, sign_callback, wait_for_receipt=True
        )
        deposit_receipt = await wait_for_transaction_receipt(
            CHAIN_ID_BASE, deposit_hash, confirmations=0
        )
        pinned = _pinned_block(deposit_receipt)

    # Pin reads to the block that included the deposit, to avoid stale RPC reads.
    v_needed = Decimal(0)
    if seed_usdc_vu > 0:
        v_needed += seed_usdc_vu / price_usdc_per_v
    if seed_prompt > 0:
        v_needed += seed_prompt * price_v_per_prompt
    if seed_agent > 0:
        v_needed += seed_agent * price_v_per_agent
    if v_needed > 0 and deposit_usdc < v_needed:
        raise RuntimeError(
            f"--deposit-usdc too small: need at least {v_needed} USDC deposited to mint enough V for seeding"
        )
    v_needed_raw = _raw(v_needed, v_decimals)
    v_bal = await _balance_after_tx(
        token_address=vault.address,
        wallet_address=wallet_address,
        pinned_block_number=pinned,
        min_expected=int(v_needed_raw),
        attempts=6,
    )
    usdc_bal = await _balance_after_tx(
        token_address=BASE_USDC,
        wallet_address=wallet_address,
        pinned_block_number=pinned,
        min_expected=0,
        attempts=3,
    )
    prompt_bal = await _balance_after_tx(
        token_address=PROMPT_BASE,
        wallet_address=wallet_address,
        pinned_block_number=pinned,
        min_expected=0,
        attempts=3,
    )
    logger.info(
        f"Balances after deposit (pinned_block={pinned}): "
        f"V={v_bal} USDC={usdc_bal} PROMPT={prompt_bal}"
    )

    # --- Uniswap v4 pools (initialize + seed liquidity) ---
    tick_spacing = 60
    hooks = ZERO_ADDRESS

    registry = await _deploy_from_file(
        rel_path="contracts/ecosystem/PoolRegistry.sol",
        contract_name="PoolRegistry",
        constructor_args=[wallet_address, PROMPT_BASE, vault.address, BASE_USDC],
        from_address=wallet_address,
        chain_id=CHAIN_ID_BASE,
        sign_callback=sign_callback,
    )
    logger.info(f"PoolRegistry: {registry.address}")

    full_tick_lower, full_tick_upper = _full_range_ticks(tick_spacing)

    vu_key = None
    if seed_usdc_vu > 0:
        # V/U: `price_usdc_per_v` USDC per 1 V (V shares are ERC20 at the vault address)
        sqrt_vu = _sqrt_price_for_pair(
            token_a=vault.address,
            token_b=BASE_USDC,
            price_b_per_a=price_usdc_per_v,
            decimals_a=v_decimals,
            decimals_b=usdc_decimals,
        )
        vu_key = build_pool_key(
            currency_a=vault.address,
            currency_b=BASE_USDC,
            fee=fee,
            tick_spacing=tick_spacing,
            hooks=hooks,
        )
        # Seed V/U: `seed_usdc_vu` USDC + matching V at `price_usdc_per_v`.
        seed_u_raw = _raw(seed_usdc_vu, usdc_decimals)
        seed_v_human = (
            (Decimal(seed_usdc_vu) / Decimal(price_usdc_per_v))
            if price_usdc_per_v != 0
            else Decimal(0)
        )
        seed_v_raw = _raw(seed_v_human, v_decimals)
        amount0_vu = (
            seed_v_raw
            if vu_key[0].lower() == vault.address.lower()
            else seed_u_raw
        )
        amount1_vu = (
            seed_u_raw
            if vu_key[1].lower() == BASE_USDC.lower()
            else seed_v_raw
        )
        liq_vu = _liq_for_full_range(
            sqrt_price_x96=sqrt_vu,
            tick_lower=full_tick_lower,
            tick_upper=full_tick_upper,
            amount0=amount0_vu,
            amount1=amount1_vu,
        )
        vu_mint = await posm_initialize_and_mint(
            chain_id=CHAIN_ID_BASE,
            position_manager_address=posm,
            permit2_address=permit2,
            key=vu_key,
            sqrt_price_x96=sqrt_vu,
            tick_lower=full_tick_lower,
            tick_upper=full_tick_upper,
            liquidity=liq_vu,
            amount0_max=amount0_vu,
            amount1_max=amount1_vu,
            recipient=wallet_address,
            from_address=wallet_address,
            sign_callback=sign_callback,
        )
        vu_id = pool_id(vu_key)
        vu_slot0 = await get_slot0(
            chain_id=CHAIN_ID_BASE,
            state_view_address=state_view,
            pool_id_=vu_id,
            block_identifier=vu_mint.get("block_number") or "latest",
        )
        logger.info(f"Seeded V/U poolId={vu_id} pos={vu_mint} slot0={vu_slot0}")

    pv_key = None
    if seed_prompt > 0:
        # P/V: bootstrap at `price_v_per_prompt` V per 1 PROMPT.
        sqrt_pv = _sqrt_price_for_pair(
            token_a=PROMPT_BASE,
            token_b=vault.address,
            price_b_per_a=price_v_per_prompt,
            decimals_a=prompt_decimals,
            decimals_b=v_decimals,
        )
        pv_key = build_pool_key(
            currency_a=PROMPT_BASE,
            currency_b=vault.address,
            fee=fee,
            tick_spacing=tick_spacing,
            hooks=hooks,
        )
        # Seed P/V: `seed_prompt` PROMPT + matching V at `price_v_per_prompt`.
        seed_p_raw = _raw(seed_prompt, prompt_decimals)
        seed_v_pv_raw = _raw(
            Decimal(seed_prompt) * Decimal(price_v_per_prompt), v_decimals
        )
        amount0_pv = (
            seed_p_raw
            if pv_key[0].lower() == PROMPT_BASE.lower()
            else seed_v_pv_raw
        )
        amount1_pv = (
            seed_v_pv_raw
            if pv_key[1].lower() == vault.address.lower()
            else seed_p_raw
        )
        liq_pv = _liq_for_full_range(
            sqrt_price_x96=sqrt_pv,
            tick_lower=full_tick_lower,
            tick_upper=full_tick_upper,
            amount0=amount0_pv,
            amount1=amount1_pv,
        )
        pv_mint = await posm_initialize_and_mint(
            chain_id=CHAIN_ID_BASE,
            position_manager_address=posm,
            permit2_address=permit2,
            key=pv_key,
            sqrt_price_x96=sqrt_pv,
            tick_lower=full_tick_lower,
            tick_upper=full_tick_upper,
            liquidity=liq_pv,
            amount0_max=amount0_pv,
            amount1_max=amount1_pv,
            recipient=wallet_address,
            from_address=wallet_address,
            sign_callback=sign_callback,
        )
        pv_id = pool_id(pv_key)
        pv_slot0 = await get_slot0(
            chain_id=CHAIN_ID_BASE,
            state_view_address=state_view,
            pool_id_=pv_id,
            block_identifier=pv_mint.get("block_number") or "latest",
        )
        logger.info(f"Seeded P/V poolId={pv_id} pos={pv_mint} slot0={pv_slot0}")

    a_decimals = await get_token_decimals(agent_token, CHAIN_ID_BASE)
    av_key = None
    if seed_agent > 0:
        # A/V: bootstrap at `price_v_per_agent` V per 1 A.
        sqrt_av = _sqrt_price_for_pair(
            token_a=agent_token,
            token_b=vault.address,
            price_b_per_a=price_v_per_agent,
            decimals_a=a_decimals,
            decimals_b=v_decimals,
        )
        av_key = build_pool_key(
            currency_a=agent_token,
            currency_b=vault.address,
            fee=fee,
            tick_spacing=tick_spacing,
            hooks=hooks,
        )
        av_tick_lower, av_tick_upper = _concentrated_ticks(
            sqrt_price_x96=sqrt_av,
            tick_spacing=tick_spacing,
            width_ticks=int(av_width_ticks),
        )
        # Seed A/V: `seed_agent` A + matching V at `price_v_per_agent`.
        seed_a_raw = _raw(seed_agent, a_decimals)
        seed_v_av_raw = _raw(
            Decimal(seed_agent) * Decimal(price_v_per_agent), v_decimals
        )
        amount0_av = (
            seed_a_raw
            if av_key[0].lower() == agent_token.lower()
            else seed_v_av_raw
        )
        amount1_av = (
            seed_v_av_raw
            if av_key[1].lower() == vault.address.lower()
            else seed_a_raw
        )
        liq_av = _liq_for_full_range(
            sqrt_price_x96=sqrt_av,
            tick_lower=av_tick_lower,
            tick_upper=av_tick_upper,
            amount0=amount0_av,
            amount1=amount1_av,
        )
        av_mint = await posm_initialize_and_mint(
            chain_id=CHAIN_ID_BASE,
            position_manager_address=posm,
            permit2_address=permit2,
            key=av_key,
            sqrt_price_x96=sqrt_av,
            tick_lower=av_tick_lower,
            tick_upper=av_tick_upper,
            liquidity=liq_av,
            amount0_max=amount0_av,
            amount1_max=amount1_av,
            recipient=locker.address,
            from_address=wallet_address,
            sign_callback=sign_callback,
        )
        av_id = pool_id(av_key)
        av_slot0 = await get_slot0(
            chain_id=CHAIN_ID_BASE,
            state_view_address=state_view,
            pool_id_=av_id,
            block_identifier=av_mint.get("block_number") or "latest",
        )
        logger.info(f"Seeded A/V poolId={av_id} pos={av_mint} slot0={av_slot0}")

        token_id = av_mint.get("token_id")
        if token_id is None:
            raise RuntimeError("A/V mint did not return token_id")

        unlock_time = (
            await _latest_timestamp(CHAIN_ID_BASE)
            + int(locker_unlock_days) * 86_400
        )
        reg_lock_tx = await encode_call(
            target=locker.address,
            abi=locker.abi,
            fn_name="registerLock",
            args=[
                int(token_id),
                av_key[0],
                av_key[1],
                locker_beneficiary,
                int(unlock_time),
                int(locker_fee_share_bps),
                int(locker_early_exit_bps),
            ],
            from_address=wallet_address,
            chain_id=CHAIN_ID_BASE,
        )
        await send_transaction(reg_lock_tx, sign_callback, wait_for_receipt=True)
        logger.info(
            f"Registered A/V lock: tokenId={token_id} beneficiary={locker_beneficiary} "
            f"unlockTime={unlock_time} feeShareBps={locker_fee_share_bps} earlyExitBps={locker_early_exit_bps}"
        )

    ap_key = None
    if seed_ap_agent > 0:
        if price_p_per_agent <= 0:
            raise RuntimeError("--price-p-per-agent must be > 0 when --seed-ap-agent > 0")

        ap_key = build_pool_key(
            currency_a=agent_token,
            currency_b=PROMPT_BASE,
            fee=int(ap_fee),
            tick_spacing=tick_spacing,
            hooks=hooks,
        )

        width = _ceil_to_spacing(int(ap_width_ticks), int(tick_spacing))
        if width <= 0:
            raise RuntimeError("--ap-width-ticks must be > 0")
        if width > int(full_tick_upper - full_tick_lower):
            raise RuntimeError("--ap-width-ticks too large for tick spacing / tick bounds")

        # Use a boundary start so we can provide only the agent token side.
        sqrt_guess = _sqrt_price_for_pair(
            token_a=agent_token,
            token_b=PROMPT_BASE,
            price_b_per_a=price_p_per_agent,
            decimals_a=a_decimals,
            decimals_b=prompt_decimals,
        )
        tick_guess = int(tick_from_sqrt_price_x96(float(sqrt_guess)))

        seed_ap_raw = _raw(seed_ap_agent, a_decimals)

        if ap_key[0].lower() == agent_token.lower():
            # Agent token is currency0. Start at lower boundary and deposit only amount0.
            start_tick = _floor_to_spacing(int(tick_guess), int(tick_spacing))
            start_tick = max(int(full_tick_lower), min(int(start_tick), int(full_tick_upper - width)))
            tick_lower = int(start_tick)
            tick_upper = int(start_tick + width)
            sqrt_start = int(sqrt_price_x96_from_tick(int(start_tick)))
            sqrt_upper = int(sqrt_price_x96_from_tick(int(tick_upper)))

            amount0_ap = int(seed_ap_raw)
            amount1_ap = 0
            liq_ap = int(liq_for_amt0(int(sqrt_start), int(sqrt_upper), int(amount0_ap)))
        else:
            # Agent token is currency1. Start at upper boundary and deposit only amount1.
            start_tick = _ceil_to_spacing(int(tick_guess), int(tick_spacing))
            start_tick = min(int(full_tick_upper), max(int(start_tick), int(full_tick_lower + width)))
            tick_upper = int(start_tick)
            tick_lower = int(start_tick - width)
            sqrt_start = int(sqrt_price_x96_from_tick(int(start_tick)))
            sqrt_lower = int(sqrt_price_x96_from_tick(int(tick_lower)))

            amount0_ap = 0
            amount1_ap = int(seed_ap_raw)
            liq_ap = int(liq_for_amt1(int(sqrt_lower), int(sqrt_start), int(amount1_ap)))

        if tick_lower >= tick_upper:
            raise RuntimeError(f"Invalid A/P tick range: lower={tick_lower} upper={tick_upper}")
        if liq_ap <= 0:
            raise RuntimeError("Computed A/P liquidity=0; increase --seed-ap-agent or widen --ap-width-ticks")

        ap_mint = await posm_initialize_and_mint(
            chain_id=CHAIN_ID_BASE,
            position_manager_address=posm,
            permit2_address=permit2,
            key=ap_key,
            sqrt_price_x96=int(sqrt_start),
            tick_lower=int(tick_lower),
            tick_upper=int(tick_upper),
            liquidity=int(liq_ap),
            amount0_max=int(amount0_ap),
            amount1_max=int(amount1_ap),
            recipient=locker.address,
            from_address=wallet_address,
            sign_callback=sign_callback,
        )
        ap_id = pool_id(ap_key)
        ap_slot0 = await get_slot0(
            chain_id=CHAIN_ID_BASE,
            state_view_address=state_view,
            pool_id_=ap_id,
            block_identifier=ap_mint.get("block_number") or "latest",
        )
        logger.info(f"Seeded A/P poolId={ap_id} pos={ap_mint} slot0={ap_slot0}")

        ap_token_id = ap_mint.get("token_id")
        if ap_token_id is None:
            raise RuntimeError("A/P mint did not return token_id")

        if ap_lock_forever:
            ap_unlock_time = 2**64 - 1
            ap_early_exit_bps = 0
        else:
            ap_unlock_time = (
                await _latest_timestamp(CHAIN_ID_BASE)
                + int(locker_unlock_days) * 86_400
            )
            ap_early_exit_bps = int(locker_early_exit_bps)

        reg_ap_lock_tx = await encode_call(
            target=locker.address,
            abi=locker.abi,
            fn_name="registerLock",
            args=[
                int(ap_token_id),
                ap_key[0],
                ap_key[1],
                locker_beneficiary,
                int(ap_unlock_time),
                int(locker_fee_share_bps),
                int(ap_early_exit_bps),
            ],
            from_address=wallet_address,
            chain_id=CHAIN_ID_BASE,
        )
        await send_transaction(reg_ap_lock_tx, sign_callback, wait_for_receipt=True)
        logger.info(
            f"Registered A/P lock: tokenId={ap_token_id} beneficiary={locker_beneficiary} "
            f"unlockTime={ap_unlock_time} feeShareBps={locker_fee_share_bps} earlyExitBps={ap_early_exit_bps}"
        )

    # Register canonical pools and the agent market in PoolRegistry.
    if pv_key is not None and vu_key is not None:
        set_base_tx = await encode_call(
            target=registry.address,
            abi=registry.abi,
            fn_name="setBasePools",
            args=[pv_key, vu_key],
            from_address=wallet_address,
            chain_id=CHAIN_ID_BASE,
        )
        await send_transaction(set_base_tx, sign_callback, wait_for_receipt=True)

    agent_pool_key = ap_key or av_key
    if agent_pool_key is None:
        raise RuntimeError("No agent pool was seeded (set --seed-agent and/or --seed-ap-agent)")

    reg_agent_tx = await encode_call(
        target=registry.address,
        abi=registry.abi,
        fn_name="registerAgentPool",
        args=[agent_token, agent_pool_key],
        from_address=wallet_address,
        chain_id=CHAIN_ID_BASE,
    )
    await send_transaction(reg_agent_tx, sign_callback, wait_for_receipt=True)

    logger.info("Dry-run complete.")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dry-run: deploy vault + agent token + initialize Uniswap v4 pools on a Base Gorlami fork."
    )
    parser.add_argument("--wallet-label", default="main")
    parser.add_argument("--fee", type=int, default=3000, help="Uniswap v4 LP fee (pips)")
    parser.add_argument("--ap-fee", type=int, default=10000, help="Uniswap v4 LP fee (pips) for A/P pool")
    parser.add_argument("--deposit-usdc", default="500", help="USDC amount to deposit into the vault (mints V)")
    parser.add_argument("--seed-usdc-vu", default="0", help="USDC amount to seed in the V/USDC pool (optional)")
    parser.add_argument("--seed-prompt", default="0", help="PROMPT amount to seed in the PROMPT/V pool (optional)")
    parser.add_argument("--seed-agent", default="1000", help="Agent token amount to seed in the A/V pool (two-sided)")
    parser.add_argument("--seed-ap-agent", default="0", help="Agent token amount to seed in the A/P pool (one-sided A-only)")
    parser.add_argument(
        "--av-width-ticks",
        default="6000",
        help="Half-width (in ticks) for concentrated A/V liquidity around initial price",
    )
    parser.add_argument(
        "--ap-width-ticks",
        default="6000",
        help="Width (in ticks) for one-sided A/P liquidity (from boundary start)",
    )
    parser.add_argument(
        "--ap-lock-forever",
        action="store_true",
        help="If set, A/P LP NFT cannot be exited (unlockTime = uint64.max).",
    )
    parser.add_argument("--locker-unlock-days", default="90", help="LP NFT lock duration in days")
    parser.add_argument("--locker-fee-share-bps", default="3000", help="Fee share to beneficiary (bps); rest to FeeVault")
    parser.add_argument("--locker-early-exit-bps", default="5000", help="Penalty to FeeVault if exit before unlock (bps)")
    parser.add_argument("--locker-beneficiary", default="", help="Address to receive beneficiary share / principal (default: wallet)")
    parser.add_argument("--price-v-per-prompt", default="0.01", help="Bootstrap price: V per 1 PROMPT")
    parser.add_argument("--price-v-per-agent", default="0.1", help="Bootstrap price: V per 1 Agent token")
    parser.add_argument("--price-p-per-agent", default="1", help="Bootstrap price: PROMPT per 1 Agent token (for A/P one-sided)")
    parser.add_argument("--price-usdc-per-v", default="1", help="Bootstrap price: USDC per 1 V")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Deploy to Base mainnet (no fork). Requires real funds.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Required with --live to confirm you want to broadcast real transactions.",
    )
    args = parser.parse_args()

    wallet = find_wallet_by_label(args.wallet_label)
    if not wallet:
        raise SystemExit(f"Wallet not found: {args.wallet_label}")

    wallet_address = to_checksum_address(wallet["address"])
    private_key = wallet.get("private_key_hex") or wallet.get("private_key")
    if not private_key:
        raise SystemExit("Wallet is missing private_key_hex in config.json")

    sign_callback = _make_sign_callback(private_key)

    deposit_usdc = Decimal(str(args.deposit_usdc))
    seed_usdc_vu = Decimal(str(args.seed_usdc_vu))
    seed_prompt = Decimal(str(args.seed_prompt))
    seed_agent = Decimal(str(args.seed_agent))
    seed_ap_agent = Decimal(str(args.seed_ap_agent))
    ap_fee = int(args.ap_fee)
    av_width_ticks = int(args.av_width_ticks)
    ap_width_ticks = int(args.ap_width_ticks)
    ap_lock_forever = bool(args.ap_lock_forever)
    locker_unlock_days = int(args.locker_unlock_days)
    locker_fee_share_bps = int(args.locker_fee_share_bps)
    locker_early_exit_bps = int(args.locker_early_exit_bps)
    locker_beneficiary = (
        to_checksum_address(args.locker_beneficiary)
        if args.locker_beneficiary
        else wallet_address
    )
    price_v_per_prompt = Decimal(str(args.price_v_per_prompt))
    price_v_per_agent = Decimal(str(args.price_v_per_agent))
    price_p_per_agent = Decimal(str(args.price_p_per_agent))
    price_usdc_per_v = Decimal(str(args.price_usdc_per_v))

    if args.live:
        if not args.yes:
            raise SystemExit("--live requires --yes to confirm broadcasting real transactions")

        if deposit_usdc < 0:
            raise SystemExit("--deposit-usdc must be >= 0")
        if seed_usdc_vu < 0 or seed_prompt < 0 or seed_agent < 0 or seed_ap_agent < 0:
            raise SystemExit("All --seed amounts must be >= 0")
        if seed_agent <= 0 and seed_ap_agent <= 0:
            raise SystemExit("Set --seed-agent and/or --seed-ap-agent to a value > 0")

        if seed_prompt > 0 and price_v_per_prompt <= 0:
            raise SystemExit("--price-v-per-prompt must be > 0 when --seed-prompt > 0")
        if seed_agent > 0 and price_v_per_agent <= 0:
            raise SystemExit("--price-v-per-agent must be > 0 when --seed-agent > 0")
        if seed_ap_agent > 0 and price_p_per_agent <= 0:
            raise SystemExit("--price-p-per-agent must be > 0 when --seed-ap-agent > 0")
        if seed_usdc_vu > 0 and price_usdc_per_v <= 0:
            raise SystemExit("--price-usdc-per-v must be > 0 when --seed-usdc-vu > 0")

        if seed_usdc_vu < 0 or seed_prompt < 0:
            raise SystemExit("--seed-usdc-vu and --seed-prompt must be >= 0")
        if seed_agent > 0 and av_width_ticks <= 0:
            raise SystemExit("--av-width-ticks must be > 0")
        if seed_ap_agent > 0 and ap_width_ticks <= 0:
            raise SystemExit("--ap-width-ticks must be > 0")
        if locker_unlock_days < 0:
            raise SystemExit("--locker-unlock-days must be >= 0")
        if not (0 <= locker_fee_share_bps <= 10_000):
            raise SystemExit("--locker-fee-share-bps must be 0..10000")
        if not (0 <= locker_early_exit_bps <= 10_000):
            raise SystemExit("--locker-early-exit-bps must be 0..10000")

        v_needed = Decimal(0)
        if seed_usdc_vu > 0:
            v_needed += seed_usdc_vu / price_usdc_per_v
        if seed_prompt > 0:
            v_needed += seed_prompt * price_v_per_prompt
        if seed_agent > 0:
            v_needed += seed_agent * price_v_per_agent
        if v_needed > 0 and deposit_usdc < v_needed:
            raise SystemExit(
                f"--deposit-usdc too small: need at least {v_needed} USDC deposited to mint enough V for seeding"
            )

        usdc_decimals = await get_token_decimals(BASE_USDC, CHAIN_ID_BASE)
        prompt_decimals = await get_token_decimals(PROMPT_BASE, CHAIN_ID_BASE)
        usdc_bal_raw = await get_token_balance(BASE_USDC, CHAIN_ID_BASE, wallet_address)
        prompt_bal_raw = await get_token_balance(PROMPT_BASE, CHAIN_ID_BASE, wallet_address)

        usdc_needed_raw = _raw(deposit_usdc + seed_usdc_vu, usdc_decimals)
        prompt_needed_raw = _raw(seed_prompt, prompt_decimals) if seed_prompt > 0 else 0

        if usdc_bal_raw < usdc_needed_raw:
            raise SystemExit(
                f"Insufficient USDC: need {deposit_usdc + seed_usdc_vu} "
                f"(raw={usdc_needed_raw}), have {_human(usdc_bal_raw, usdc_decimals)} "
                f"(raw={usdc_bal_raw})"
            )
        if seed_prompt > 0 and prompt_bal_raw < prompt_needed_raw:
            raise SystemExit(
                f"Insufficient PROMPT: need {seed_prompt} "
                f"(raw={prompt_needed_raw}), have {_human(prompt_bal_raw, prompt_decimals)} "
                f"(raw={prompt_bal_raw})"
            )

        return await _run_flow(
            wallet_address=wallet_address,
            sign_callback=sign_callback,
            fee=int(args.fee),
            ap_fee=int(ap_fee),
            deposit_usdc=deposit_usdc,
            seed_usdc_vu=seed_usdc_vu,
            seed_prompt=seed_prompt,
            seed_agent=seed_agent,
            seed_ap_agent=seed_ap_agent,
            av_width_ticks=av_width_ticks,
            ap_width_ticks=ap_width_ticks,
            ap_lock_forever=ap_lock_forever,
            locker_unlock_days=locker_unlock_days,
            locker_fee_share_bps=locker_fee_share_bps,
            locker_early_exit_bps=locker_early_exit_bps,
            locker_beneficiary=locker_beneficiary,
            price_v_per_prompt=price_v_per_prompt,
            price_v_per_agent=price_v_per_agent,
            price_p_per_agent=price_p_per_agent,
            price_usdc_per_v=price_usdc_per_v,
        )

    logger.info("Creating Base fork (Gorlami)...")
    async with gorlami_fork(
        CHAIN_ID_BASE,
        native_balances={wallet_address: 10 * 10**18},
        erc20_balances=[
            (BASE_USDC, wallet_address, 1_000_000 * 10**6),
            # Seed PROMPT with a large raw balance (decimals are read later).
            (PROMPT_BASE, wallet_address, 1_000_000 * 10**18),
        ],
    ) as (_client, fork_info):
        logger.info(f"Fork RPC: {fork_info.get('rpc_url')}")
        return await _run_flow(
            wallet_address=wallet_address,
            sign_callback=sign_callback,
            fee=int(args.fee),
            ap_fee=int(ap_fee),
            deposit_usdc=deposit_usdc,
            seed_usdc_vu=seed_usdc_vu,
            seed_prompt=seed_prompt,
            seed_agent=seed_agent,
            seed_ap_agent=seed_ap_agent,
            av_width_ticks=av_width_ticks,
            ap_width_ticks=ap_width_ticks,
            ap_lock_forever=ap_lock_forever,
            locker_unlock_days=locker_unlock_days,
            locker_fee_share_bps=locker_fee_share_bps,
            locker_early_exit_bps=locker_early_exit_bps,
            locker_beneficiary=locker_beneficiary,
            price_v_per_prompt=price_v_per_prompt,
            price_v_per_agent=price_v_per_agent,
            price_p_per_agent=price_p_per_agent,
            price_usdc_per_v=price_usdc_per_v,
        )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
