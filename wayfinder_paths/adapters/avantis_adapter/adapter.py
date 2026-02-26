from __future__ import annotations

import asyncio
from typing import Any

from eth_utils import to_checksum_address
from web3.exceptions import ContractLogicError, Web3RPCError

from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter
from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT
from wayfinder_paths.core.constants.avantis_abi import AVANTIS_VAULT_MANAGER_ABI
from wayfinder_paths.core.constants.base import MAX_UINT256
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE
from wayfinder_paths.core.constants.contracts import (
    AVANTIS_AVUSDC,
    AVANTIS_VAULT_MANAGER,
    BASE_USDC,
)
from wayfinder_paths.core.constants.erc4626_abi import ERC4626_ABI
from wayfinder_paths.core.utils.evm_helpers import maybe_checksum
from wayfinder_paths.core.utils.tokens import ensure_allowance
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

CHAIN_NAME = "base"


class AvantisAdapter(BaseAdapter):
    """Adapter for the Avantis avUSDC (ERC-4626) LP vault on Base.

    - `deposit(amount)` — ERC-4626 `deposit(assets, receiver)` (assets = USDC base units).
    - `withdraw(amount)` — ERC-4626 `redeem(shares, receiver, owner)` (shares = avUSDC base units).
    """

    adapter_type = "AVANTIS"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        sign_callback: Any | None = None,
        wallet_address: str | None = None,
    ) -> None:
        super().__init__("avantis_adapter", config)

        self.sign_callback = sign_callback

        self.chain_id = CHAIN_ID_BASE
        self.chain_name = CHAIN_NAME

        self.vault: str = AVANTIS_AVUSDC
        self.vault_manager: str = AVANTIS_VAULT_MANAGER
        self.underlying: str = BASE_USDC

        self.wallet_address: str | None = maybe_checksum(wallet_address)

    async def get_all_markets(self) -> tuple[bool, list[dict[str, Any]] | str]:
        """Return the configured Avantis vault as a single-market list."""
        try:
            async with web3_from_chain_id(self.chain_id) as web3:
                v = web3.eth.contract(address=self.vault, abi=ERC4626_ABI)

                asset_coro = v.functions.asset().call(block_identifier="pending")
                decimals_coro = v.functions.decimals().call(block_identifier="pending")
                symbol_coro = v.functions.symbol().call(block_identifier="pending")
                name_coro = v.functions.name().call(block_identifier="pending")
                total_assets_coro = v.functions.totalAssets().call(
                    block_identifier="pending"
                )
                total_supply_coro = v.functions.totalSupply().call(
                    block_identifier="pending"
                )

                (
                    asset,
                    decimals,
                    symbol,
                    name,
                    total_assets,
                    total_supply,
                ) = await asyncio.gather(
                    asset_coro,
                    decimals_coro,
                    symbol_coro,
                    name_coro,
                    total_assets_coro,
                    total_supply_coro,
                )

                share_decimals = int(decimals or 0)
                unit_shares = 10**share_decimals if share_decimals >= 0 else 0
                try:
                    share_price = (
                        await v.functions.convertToAssets(unit_shares).call(
                            block_identifier="pending"
                        )
                        if unit_shares
                        else 0
                    )
                except (ContractLogicError, Web3RPCError):
                    share_price = 0

                market: dict[str, Any] = {
                    "chain_id": int(self.chain_id),
                    "vault": self.vault,
                    "underlying": to_checksum_address(str(asset)),
                    "symbol": str(symbol or ""),
                    "name": str(name or ""),
                    "decimals": share_decimals,
                    "total_assets": int(total_assets or 0),
                    "total_supply": int(total_supply or 0),
                    # assets per 1.0 share, scaled by underlying decimals
                    "share_price": int(share_price or 0),
                    "tvl": int(total_assets or 0),
                }
                return True, [market]
        except Exception as exc:
            return False, str(exc)

    async def get_vault_manager_state(
        self,
        *,
        block_identifier: int | str | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        block_id = block_identifier if block_identifier is not None else "pending"
        try:
            async with web3_from_chain_id(self.chain_id) as web3:
                mgr = web3.eth.contract(
                    address=self.vault_manager, abi=AVANTIS_VAULT_MANAGER_ABI
                )
                (
                    junior,
                    senior,
                    bal,
                    adj_bal,
                    buffer_ratio,
                    total_rewards,
                    pnl_rewards,
                    reward_period,
                    last_reward_time,
                ) = await asyncio.gather(
                    mgr.functions.junior().call(block_identifier=block_id),
                    mgr.functions.senior().call(block_identifier=block_id),
                    mgr.functions.currentBalanceUSDC().call(block_identifier=block_id),
                    mgr.functions.currentAdjustedBalanceUSDC().call(
                        block_identifier=block_id
                    ),
                    mgr.functions.getBufferRatio().call(block_identifier=block_id),
                    mgr.functions.totalRewards().call(block_identifier=block_id),
                    mgr.functions.pnlRewards().call(block_identifier=block_id),
                    mgr.functions.rewardPeriod().call(block_identifier=block_id),
                    mgr.functions.lastRewardTime().call(block_identifier=block_id),
                )
                return (
                    True,
                    {
                        "vault_manager": self.vault_manager,
                        "junior": to_checksum_address(str(junior)),
                        "senior": to_checksum_address(str(senior)),
                        "currentBalanceUSDC": int(bal or 0),
                        "currentAdjustedBalanceUSDC": int(adj_bal or 0),
                        "bufferRatio": int(buffer_ratio or 0),
                        "totalRewards": int(total_rewards or 0),
                        "pnlRewards": int(pnl_rewards or 0),
                        "rewardPeriod": int(reward_period or 0),
                        "lastRewardTime": int(last_reward_time or 0),
                    },
                )
        except Exception as exc:
            return False, str(exc)

    async def deposit(
        self,
        *,
        vault_address: str | None = None,
        underlying_token: str | None = None,
        amount: int,
    ) -> tuple[bool, Any]:
        wallet = self.wallet_address
        if not wallet:
            return False, "wallet_address is required"
        if not self.sign_callback:
            return False, "sign_callback is required"

        assets = int(amount)
        if assets <= 0:
            return False, "amount must be positive"

        vault = to_checksum_address(vault_address) if vault_address else self.vault
        asset = (
            to_checksum_address(underlying_token)
            if underlying_token
            else self.underlying
        )

        try:
            approved = await ensure_allowance(
                token_address=asset,
                owner=wallet,
                spender=vault,
                amount=assets,
                chain_id=self.chain_id,
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved[0]:
                return approved

            transaction = await encode_call(
                target=vault,
                abi=ERC4626_ABI,
                fn_name="deposit",
                args=[assets, wallet],
                from_address=wallet,
                chain_id=self.chain_id,
            )
            txn_hash = await send_transaction(transaction, self.sign_callback)
            return True, txn_hash
        except Exception as exc:
            return False, str(exc)

    async def withdraw(
        self,
        *,
        vault_address: str | None = None,
        amount: int,
        redeem_full: bool = False,
    ) -> tuple[bool, Any]:
        wallet = self.wallet_address
        if not wallet:
            return False, "wallet_address is required"
        if not self.sign_callback:
            return False, "sign_callback is required"

        vault = to_checksum_address(vault_address) if vault_address else self.vault

        try:
            shares = int(amount)

            if redeem_full:
                async with web3_from_chain_id(self.chain_id) as web3:
                    v = web3.eth.contract(address=vault, abi=ERC4626_ABI)
                    try:
                        shares = await v.functions.maxRedeem(wallet).call(
                            block_identifier="pending"
                        )
                    except (ContractLogicError, Web3RPCError):
                        shares = await v.functions.balanceOf(wallet).call(
                            block_identifier="pending"
                        )

                shares = int(shares or 0)
                if shares <= 0:
                    return True, "no shares to redeem"
            else:
                if shares <= 0:
                    return False, "amount must be positive"

            transaction = await encode_call(
                target=vault,
                abi=ERC4626_ABI,
                fn_name="redeem",
                args=[shares, wallet, wallet],
                from_address=wallet,
                chain_id=self.chain_id,
            )
            txn_hash = await send_transaction(transaction, self.sign_callback)
            return True, txn_hash
        except Exception as exc:
            return False, str(exc)

    async def borrow(self, **_kwargs: Any) -> tuple[bool, str]:
        return False, "Avantis LP vault does not support user borrow()"

    async def repay(self, **_kwargs: Any) -> tuple[bool, str]:
        return False, "Avantis LP vault does not support user repay()"

    async def get_pos(
        self,
        *,
        vault_address: str | None = None,
        account: str | None = None,
        include_usd: bool = False,
        block_identifier: int | str | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        vault = to_checksum_address(vault_address) if vault_address else self.vault
        acct = to_checksum_address(account) if account else self.wallet_address
        if not acct:
            return False, "wallet_address is required"
        block_id = block_identifier if block_identifier is not None else "pending"

        try:
            async with web3_from_chain_id(self.chain_id) as web3:
                v = web3.eth.contract(address=vault, abi=ERC4626_ABI)

                decimals_coro = v.functions.decimals().call(block_identifier=block_id)
                asset_coro = v.functions.asset().call(block_identifier=block_id)
                shares_coro = v.functions.balanceOf(acct).call(
                    block_identifier=block_id
                )
                total_assets_coro = v.functions.totalAssets().call(
                    block_identifier=block_id
                )
                total_supply_coro = v.functions.totalSupply().call(
                    block_identifier=block_id
                )
                max_redeem_coro = v.functions.maxRedeem(acct).call(
                    block_identifier=block_id
                )
                max_withdraw_coro = v.functions.maxWithdraw(acct).call(
                    block_identifier=block_id
                )

                (
                    decimals,
                    asset,
                    shares,
                    total_assets,
                    total_supply,
                    max_redeem,
                    max_withdraw,
                ) = await asyncio.gather(
                    decimals_coro,
                    asset_coro,
                    shares_coro,
                    total_assets_coro,
                    total_supply_coro,
                    max_redeem_coro,
                    max_withdraw_coro,
                )

                shares_i = int(shares or 0)
                assets_i = (
                    int(
                        await v.functions.convertToAssets(shares_i).call(
                            block_identifier=block_id
                        )
                    )
                    if shares_i > 0
                    else 0
                )

                share_decimals = int(decimals or 0)
                unit_shares = 10**share_decimals if share_decimals >= 0 else 0
                try:
                    share_price = (
                        int(
                            await v.functions.convertToAssets(unit_shares).call(
                                block_identifier=block_id
                            )
                        )
                        if unit_shares
                        else 0
                    )
                except (ContractLogicError, Web3RPCError):
                    share_price = 0

                underlying = to_checksum_address(str(asset))
        except Exception as exc:
            return False, str(exc)

        try:
            vault_key = f"{self.chain_name}_{vault}"
            underlying_key = f"{self.chain_name}_{underlying}"

            balances: dict[str, int] = {vault_key: int(shares_i)}
            result: dict[str, Any] = {
                "balances": balances,
                "shares_balance": int(shares_i),
                "assets_balance": int(assets_i),
                "underlying_token": underlying,
                "share_price": int(share_price),
                "max_redeem": int(max_redeem or 0),
                "max_withdraw": int(max_withdraw or 0),
                "total_assets": int(total_assets or 0),
                "total_supply": int(total_supply or 0),
                "decimals": int(share_decimals),
            }

            if include_usd:
                usd_val = await self._usd_value(
                    token_key=underlying_key, amount_raw=int(assets_i)
                )
                result["usd_balances"] = {
                    vault_key: usd_val,
                    underlying_key: usd_val,
                }
                result["usd_value"] = usd_val

            return True, result
        except Exception as exc:
            return False, str(exc)

    async def get_full_user_state(
        self,
        *,
        account: str,
        include_zero_positions: bool = False,
        include_usd: bool = False,
        block_identifier: int | str | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        acct = to_checksum_address(account)

        ok, pos = await self.get_pos(
            vault_address=self.vault,
            account=acct,
            include_usd=include_usd,
            block_identifier=block_identifier,
        )
        if not ok:
            return False, str(pos)
        assert isinstance(pos, dict)

        shares = int(pos.get("shares_balance") or 0)
        assets = int(pos.get("assets_balance") or 0)
        if not include_zero_positions and shares <= 0 and assets <= 0:
            positions: list[dict[str, Any]] = []
        else:
            positions = [
                {
                    "vault": self.vault,
                    "underlying": self.underlying,
                    "shares": shares,
                    "assets": assets,
                    "share_price": int(pos.get("share_price") or 0),
                    "max_redeem": int(pos.get("max_redeem") or 0),
                    "max_withdraw": int(pos.get("max_withdraw") or 0),
                }
            ]

        state: dict[str, Any] = {
            "protocol": "avantis",
            "chainId": int(self.chain_id),
            "account": acct,
            "positions": positions,
        }
        if include_usd:
            state["usd_value"] = pos.get("usd_value")
        return True, state

    async def _usd_value(self, *, token_key: str, amount_raw: int) -> float | None:
        try:
            data = await TOKEN_CLIENT.get_token_details(token_key, market_data=True)
            price = (
                data.get("price_usd") or data.get("price") or data.get("current_price")
            )
            if not price:
                return None
            decimals = int(data.get("decimals", 18))
            return (float(amount_raw) / (10**decimals)) * float(price)
        except Exception:
            return None
