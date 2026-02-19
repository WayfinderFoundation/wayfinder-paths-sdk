#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from eth_account import Account
from eth_utils import to_checksum_address
from loguru import logger

from wayfinder_paths.core.constants.contracts import (
    BASE_USDC,
    UNISWAP_V4_POOL_MANAGER,
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
from wayfinder_paths.core.utils.uniswap_v3_math import price_to_sqrt_price_x96
from wayfinder_paths.core.utils.uniswap_v4_deploy import (
    build_pool_key,
    get_slot0,
    initialize_pool,
    pool_id,
    sort_currencies,
)
from wayfinder_paths.core.utils.web3 import web3_from_chain_id
from wayfinder_paths.mcp.utils import find_wallet_by_label

CHAIN_ID_BASE = 8453
PROMPT_BASE = to_checksum_address("0x30c7235866872213f68cb1f08c37cb9eccb93452")
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def _raw(amount: str | int | Decimal, decimals: int) -> int:
    return int(Decimal(str(amount)) * (Decimal(10) ** int(decimals)))


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


@dataclass(frozen=True)
class DeployedContract:
    address: str
    abi: list[dict[str, Any]]


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


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dry-run: deploy vault + agent token + initialize Uniswap v4 pools on a Base Gorlami fork."
    )
    parser.add_argument("--wallet-label", default="main")
    parser.add_argument("--fee", type=int, default=3000, help="Uniswap v4 LP fee (pips)")
    args = parser.parse_args()

    wallet = find_wallet_by_label(args.wallet_label)
    if not wallet:
        raise SystemExit(f"Wallet not found: {args.wallet_label}")

    wallet_address = to_checksum_address(wallet["address"])
    private_key = wallet.get("private_key_hex") or wallet.get("private_key")
    if not private_key:
        raise SystemExit("Wallet is missing private_key_hex in config.json")

    sign_callback = _make_sign_callback(private_key)

    pool_manager = UNISWAP_V4_POOL_MANAGER[CHAIN_ID_BASE]
    state_view = UNISWAP_V4_STATE_VIEW[CHAIN_ID_BASE]

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

        async with web3_from_chain_id(CHAIN_ID_BASE) as w3:
            code = await w3.eth.get_code(PROMPT_BASE)
            if not code:
                raise RuntimeError(
                    f"PROMPT contract not found on Base: {PROMPT_BASE}"
                )

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
        async with web3_from_chain_id(CHAIN_ID_BASE) as w3:
            receipt = await w3.eth.get_transaction_receipt(create_hash)
            factory_contract = w3.eth.contract(address=factory_ct.address, abi=factory_ct.abi)
            evs = factory_contract.events.AgentTokenCreated().process_receipt(receipt)
            if not evs:
                raise RuntimeError("AgentTokenCreated event not found in receipt")
            agent_token = to_checksum_address(evs[0]["args"]["token"])

        logger.info(f"Agent token (A): {agent_token}")

        # Mint V shares by depositing USDC into the vault.
        deposit_usdc = _raw("500000", usdc_decimals)
        await ensure_allowance(
            token_address=BASE_USDC,
            owner=wallet_address,
            spender=vault.address,
            amount=int(deposit_usdc),
            chain_id=CHAIN_ID_BASE,
            signing_callback=sign_callback,
            approval_amount=int(deposit_usdc),
        )
        deposit_tx = await encode_call(
            target=vault.address,
            abi=vault.abi,
            fn_name="deposit",
            args=[int(deposit_usdc), wallet_address],
            from_address=wallet_address,
            chain_id=CHAIN_ID_BASE,
        )
        await send_transaction(deposit_tx, sign_callback, wait_for_receipt=True)

        v_bal = await get_token_balance(vault.address, CHAIN_ID_BASE, wallet_address)
        usdc_bal = await get_token_balance(BASE_USDC, CHAIN_ID_BASE, wallet_address)
        prompt_bal = await get_token_balance(PROMPT_BASE, CHAIN_ID_BASE, wallet_address)
        logger.info(
            f"Balances after deposit: V={v_bal} USDC={usdc_bal} PROMPT={prompt_bal}"
        )

        # --- Uniswap v4 pools (initialize only) ---
        fee = int(args.fee)
        tick_spacing = 60
        hooks = ZERO_ADDRESS

        if CHAIN_ID_BASE not in UNISWAP_V4_POOL_MANAGER or CHAIN_ID_BASE not in UNISWAP_V4_STATE_VIEW:
            raise RuntimeError("Missing Uniswap v4 addresses for Base in constants")

        registry = await _deploy_from_file(
            rel_path="contracts/ecosystem/PoolRegistry.sol",
            contract_name="PoolRegistry",
            constructor_args=[wallet_address, PROMPT_BASE, vault.address, BASE_USDC],
            from_address=wallet_address,
            chain_id=CHAIN_ID_BASE,
            sign_callback=sign_callback,
        )
        logger.info(f"PoolRegistry: {registry.address}")

        # V/U: 1 V ~= 1 USDC (V shares are ERC20 at the vault address)
        sqrt_vu = _sqrt_price_for_pair(
            token_a=vault.address,
            token_b=BASE_USDC,
            price_b_per_a=Decimal(1),
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
        vu_hash = await initialize_pool(
            chain_id=CHAIN_ID_BASE,
            pool_manager_address=pool_manager,
            key=vu_key,
            sqrt_price_x96=sqrt_vu,
            from_address=wallet_address,
            sign_callback=sign_callback,
        )
        vu_id = pool_id(vu_key)
        vu_slot0 = await get_slot0(chain_id=CHAIN_ID_BASE, state_view_address=state_view, pool_id_=vu_id)
        logger.info(f"Initialized V/U poolId={vu_id} tx={vu_hash} slot0={vu_slot0}")

        # P/V: set 1 PROMPT = 0.01 V for bootstrap
        sqrt_pv = _sqrt_price_for_pair(
            token_a=PROMPT_BASE,
            token_b=vault.address,
            price_b_per_a=Decimal("0.01"),
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
        pv_hash = await initialize_pool(
            chain_id=CHAIN_ID_BASE,
            pool_manager_address=pool_manager,
            key=pv_key,
            sqrt_price_x96=sqrt_pv,
            from_address=wallet_address,
            sign_callback=sign_callback,
        )
        pv_id = pool_id(pv_key)
        pv_slot0 = await get_slot0(chain_id=CHAIN_ID_BASE, state_view_address=state_view, pool_id_=pv_id)
        logger.info(f"Initialized P/V poolId={pv_id} tx={pv_hash} slot0={pv_slot0}")

        # A/V: set 1 A = 0.1 V for bootstrap
        a_decimals = await get_token_decimals(agent_token, CHAIN_ID_BASE)
        sqrt_av = _sqrt_price_for_pair(
            token_a=agent_token,
            token_b=vault.address,
            price_b_per_a=Decimal("0.1"),
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
        av_hash = await initialize_pool(
            chain_id=CHAIN_ID_BASE,
            pool_manager_address=pool_manager,
            key=av_key,
            sqrt_price_x96=sqrt_av,
            from_address=wallet_address,
            sign_callback=sign_callback,
        )
        av_id = pool_id(av_key)
        av_slot0 = await get_slot0(chain_id=CHAIN_ID_BASE, state_view_address=state_view, pool_id_=av_id)
        logger.info(f"Initialized A/V poolId={av_id} tx={av_hash} slot0={av_slot0}")

        # Register canonical pools and the agent market in PoolRegistry.
        set_base_tx = await encode_call(
            target=registry.address,
            abi=registry.abi,
            fn_name="setBasePools",
            args=[pv_key, vu_key],
            from_address=wallet_address,
            chain_id=CHAIN_ID_BASE,
        )
        await send_transaction(set_base_tx, sign_callback, wait_for_receipt=True)

        reg_agent_tx = await encode_call(
            target=registry.address,
            abi=registry.abi,
            fn_name="registerAgentPool",
            args=[agent_token, av_key],
            from_address=wallet_address,
            chain_id=CHAIN_ID_BASE,
        )
        await send_transaction(reg_agent_tx, sign_callback, wait_for_receipt=True)

        logger.info("Dry-run complete.")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
