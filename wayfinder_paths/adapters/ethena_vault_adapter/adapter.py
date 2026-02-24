from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from eth_utils import to_checksum_address

from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter
from wayfinder_paths.core.constants.base import MAX_UINT256, SECONDS_PER_YEAR
from wayfinder_paths.core.constants.chains import CHAIN_ID_ETHEREUM
from wayfinder_paths.core.constants.ethena_abi import ETHENA_SUSDE_VAULT_ABI
from wayfinder_paths.core.constants.ethena_contracts import (
    ETHENA_SUSDE_VAULT_MAINNET,
    ETHENA_USDE_MAINNET,
    ethena_tokens_by_chain_id,
)
from wayfinder_paths.core.utils.interest import apr_to_apy
from wayfinder_paths.core.utils.tokens import ensure_allowance, get_token_balance
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

VESTING_PERIOD_S = 8 * 60 * 60  # 8 hours
 

class EthenaVaultAdapter(BaseAdapter):
    """
    Ethena sUSDe staking vault adapter (canonical vault on Ethereum mainnet).

    - Deposit: ERC-4626 `deposit` (stake USDe -> receive sUSDe shares)
    - Withdraw: two-step cooldown (`cooldownShares`/`cooldownAssets`) then `unstake`
    """

    adapter_type = "ETHENA"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        sign_callback: Callable | None = None,
        wallet_address: str | None = None,
    ) -> None:
        super().__init__("ethena_vault_adapter", config)
        self.sign_callback = sign_callback
        self.wallet_address: str | None = (
            to_checksum_address(wallet_address) if wallet_address else None
        )

    async def get_apy(self) -> tuple[bool, float | str]:
        """
        Compute a "spot" supply APY from Ethena's linear vesting model.

        Ethena rewards vest linearly over ~8 hours. We estimate the current
        per-second asset growth as:

            vesting_rate_assets_per_sec = unvested / remaining
            growth_per_sec = vesting_rate_assets_per_sec / total_assets
        """
        try:
            async with web3_from_chain_id(CHAIN_ID_ETHEREUM) as web3:
                vault = web3.eth.contract(
                    address=ETHENA_SUSDE_VAULT_MAINNET,
                    abi=ETHENA_SUSDE_VAULT_ABI,
                )

                unvested_coro = vault.functions.getUnvestedAmount().call(
                    block_identifier="pending"
                )
                last_dist_coro = vault.functions.lastDistributionTimestamp().call(
                    block_identifier="pending"
                )
                total_assets_coro = vault.functions.totalAssets().call(
                    block_identifier="pending"
                )
                block_coro = web3.eth.get_block("latest")

                unvested, last_dist, total_assets, block = await asyncio.gather(
                    unvested_coro, last_dist_coro, total_assets_coro, block_coro
                )

                unvested_i = int(unvested or 0)
                total_assets_i = int(total_assets or 0)
                if unvested_i <= 0 or total_assets_i <= 0:
                    return True, 0.0

                now_ts = int(block.get("timestamp") or 0)
                last_ts = int(last_dist or 0)
                elapsed = max(0, now_ts - last_ts)
                remaining = max(0, VESTING_PERIOD_S - elapsed)
                if remaining <= 0:
                    return True, 0.0

                vesting_rate_assets_per_s = unvested_i / float(remaining)
                apr = (vesting_rate_assets_per_s / float(total_assets_i)) * float(
                    SECONDS_PER_YEAR
                )
                apy = apr_to_apy(apr)
                return True, float(apy)
        except Exception as exc:
            return False, str(exc)

    async def get_cooldown(
        self,
        *,
        account: str,
    ) -> tuple[bool, dict[str, int] | str]:
        acct = to_checksum_address(account)
        try:
            async with web3_from_chain_id(CHAIN_ID_ETHEREUM) as web3:
                vault = web3.eth.contract(
                    address=ETHENA_SUSDE_VAULT_MAINNET,
                    abi=ETHENA_SUSDE_VAULT_ABI,
                )
                cooldown_end, underlying_amount = await vault.functions.cooldowns(
                    acct
                ).call(block_identifier="pending")
                return True, {
                    "cooldownEnd": int(cooldown_end or 0),
                    "underlyingAmount": int(underlying_amount or 0),
                }
        except Exception as exc:  
            return False, str(exc)

    async def get_full_user_state(
        self,
        *,
        account: str,
        chain_id: int = CHAIN_ID_ETHEREUM,
        include_apy: bool = False,
        include_zero_positions: bool = False,
        block_identifier: int | str = "pending",
    ) -> tuple[bool, dict[str, Any] | str]:
        acct = to_checksum_address(account)
        cid = int(chain_id)

        try:
            token_addrs = ethena_tokens_by_chain_id(cid)
            usde_addr = token_addrs["usde"]
            susde_addr = token_addrs["susde"]

            if cid == CHAIN_ID_ETHEREUM:
                async with web3_from_chain_id(CHAIN_ID_ETHEREUM) as web3:
                    vault = web3.eth.contract(
                        address=ETHENA_SUSDE_VAULT_MAINNET,
                        abi=ETHENA_SUSDE_VAULT_ABI,
                    )
                    (
                        usde_balance,
                        susde_balance,
                        cooldown_raw,
                    ) = await asyncio.gather(
                        get_token_balance(
                            usde_addr,
                            cid,
                            acct,
                            web3=web3,
                            block_identifier=block_identifier,
                        ),
                        get_token_balance(
                            susde_addr,
                            cid,
                            acct,
                            web3=web3,
                            block_identifier=block_identifier,
                        ),
                        vault.functions.cooldowns(acct).call(
                            block_identifier="pending"
                        ),
                    )
                    cooldown = {
                        "cooldownEnd": int(cooldown_raw[0] or 0),
                        "underlyingAmount": int(cooldown_raw[1] or 0),
                    }
                    shares = int(susde_balance or 0)
                    usde_equivalent = 0
                    if shares > 0:
                        usde_equivalent = int(
                            await vault.functions.convertToAssets(shares).call(
                                block_identifier="pending"
                            )
                            or 0
                        )
            else:
                # Balances on the target chain, vault reads on mainnet.
                async with web3_from_chain_id(cid) as web3:
                    usde_balance, susde_balance = await asyncio.gather(
                        get_token_balance(
                            usde_addr,
                            cid,
                            acct,
                            web3=web3,
                            block_identifier=block_identifier,
                        ),
                        get_token_balance(
                            susde_addr,
                            cid,
                            acct,
                            web3=web3,
                            block_identifier=block_identifier,
                        ),
                    )

                shares = int(susde_balance or 0)

                async with web3_from_chain_id(CHAIN_ID_ETHEREUM) as web3_hub:
                    vault = web3_hub.eth.contract(
                        address=ETHENA_SUSDE_VAULT_MAINNET,
                        abi=ETHENA_SUSDE_VAULT_ABI,
                    )
                    coros: list[Any] = [
                        vault.functions.cooldowns(acct).call(
                            block_identifier="pending"
                        ),
                    ]
                    if shares > 0:
                        coros.append(
                            vault.functions.convertToAssets(shares).call(
                                block_identifier="pending"
                            )
                        )
                    results = await asyncio.gather(*coros)
                    cooldown_raw = results[0]
                    cooldown = {
                        "cooldownEnd": int(cooldown_raw[0] or 0),
                        "underlyingAmount": int(cooldown_raw[1] or 0),
                    }
                    usde_equivalent = int(results[1] or 0) if shares > 0 else 0

            cd_underlying = cooldown.get("underlyingAmount", 0)

            apy_supply: float | None = None
            if include_apy:
                ok_apy, apy_val = await self.get_apy()
                if ok_apy and isinstance(apy_val, (float, int)):
                    apy_supply = float(apy_val)

            include_position = include_zero_positions or shares > 0 or cd_underlying > 0

            positions: list[dict[str, Any]] = []
            if include_position:
                positions.append(
                    {
                        "chainId": cid,
                        "usde": usde_addr,
                        "susde": susde_addr,
                        "usdeBalance": int(usde_balance or 0),
                        "susdeBalance": shares,
                        "usdeEquivalent": usde_equivalent,
                        "cooldown": cooldown,
                        "apySupply": apy_supply,
                        "apyBorrow": None,
                    }
                )

            return (
                True,
                {
                    "protocol": "ethena",
                    "hubChainId": CHAIN_ID_ETHEREUM,
                    "chainId": cid,
                    "account": acct,
                    "positions": positions,
                },
            )
        except Exception as exc:  
            return False, str(exc)

    async def deposit_usde(
        self,
        *,
        amount_assets: int,
        receiver: str | None = None,
    ) -> tuple[bool, Any]:
        strategy = self.wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"
        if amount_assets <= 0:
            return False, "amount_assets must be positive"

        recv = to_checksum_address(receiver) if receiver else strategy

        try:
            approved = await ensure_allowance(
                token_address=ETHENA_USDE_MAINNET,
                owner=strategy,
                spender=ETHENA_SUSDE_VAULT_MAINNET,
                amount=amount_assets,
                chain_id=CHAIN_ID_ETHEREUM,
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved[0]:
                return approved

            tx = await encode_call(
                target=ETHENA_SUSDE_VAULT_MAINNET,
                abi=ETHENA_SUSDE_VAULT_ABI,
                fn_name="deposit",
                args=[amount_assets, recv],
                from_address=strategy,
                chain_id=CHAIN_ID_ETHEREUM,
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:  
            return False, str(exc)

    async def request_withdraw_by_shares(
        self,
        *,
        shares: int,
    ) -> tuple[bool, Any]:
        strategy = self.wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"
        if shares <= 0:
            return False, "shares must be positive"

        try:
            tx = await encode_call(
                target=ETHENA_SUSDE_VAULT_MAINNET,
                abi=ETHENA_SUSDE_VAULT_ABI,
                fn_name="cooldownShares",
                args=[shares],
                from_address=strategy,
                chain_id=CHAIN_ID_ETHEREUM,
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:  
            return False, str(exc)

    async def request_withdraw_by_assets(
        self,
        *,
        assets: int,
    ) -> tuple[bool, Any]:
        strategy = self.wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"
        if assets <= 0:
            return False, "assets must be positive"

        try:
            tx = await encode_call(
                target=ETHENA_SUSDE_VAULT_MAINNET,
                abi=ETHENA_SUSDE_VAULT_ABI,
                fn_name="cooldownAssets",
                args=[assets],
                from_address=strategy,
                chain_id=CHAIN_ID_ETHEREUM,
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:  
            return False, str(exc)

    async def claim_withdraw(
        self,
        *,
        receiver: str | None = None,
        require_matured: bool = True,
    ) -> tuple[bool, Any]:
        strategy = self.wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"

        recv = to_checksum_address(receiver) if receiver else strategy

        try:
            ok_cd, cd = await self.get_cooldown(account=strategy)
            if not ok_cd:
                return False, str(cd)
            if not isinstance(cd, dict):
                return False, "unexpected cooldown payload"

            cooldown_end = int(cd.get("cooldownEnd") or 0)
            underlying_amount = int(cd.get("underlyingAmount") or 0)
            if underlying_amount <= 0:
                return True, "no pending cooldown"

            if require_matured and cooldown_end > 0:
                async with web3_from_chain_id(CHAIN_ID_ETHEREUM) as web3:
                    block = await web3.eth.get_block("latest")
                    now_ts = int(block.get("timestamp") or 0)
                if now_ts < cooldown_end:
                    return (
                        False,
                        f"Cooldown not finished (now={now_ts}, cooldownEnd={cooldown_end})",
                    )

            tx = await encode_call(
                target=ETHENA_SUSDE_VAULT_MAINNET,
                abi=ETHENA_SUSDE_VAULT_ABI,
                fn_name="unstake",
                args=[recv],
                from_address=strategy,
                chain_id=CHAIN_ID_ETHEREUM,
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:  
            return False, str(exc)
