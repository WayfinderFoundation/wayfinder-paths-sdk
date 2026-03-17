from __future__ import annotations

import asyncio
from typing import Any

from eth_utils import to_checksum_address

from wayfinder_paths.adapters.aave_v3_adapter.adapter import AaveV3Adapter
from wayfinder_paths.core.adapters.BaseAdapter import require_wallet
from wayfinder_paths.core.constants import ZERO_ADDRESS
from wayfinder_paths.core.constants.base import MAX_UINT256
from wayfinder_paths.core.constants.sparklend_abi import (
    POOL_ABI,
    PROTOCOL_DATA_PROVIDER_ABI,
    REWARDS_CONTROLLER_ABI,
    WETH_GATEWAY_ABI,
)
from wayfinder_paths.core.constants.sparklend_contracts import SPARKLEND_BY_CHAIN
from wayfinder_paths.core.utils import web3 as web3_utils
from wayfinder_paths.core.utils.interest import apr_to_apy, ray_to_apr
from wayfinder_paths.core.utils.multicall import (
    Call,
    read_only_calls_multicall_or_gather,
)
from wayfinder_paths.core.utils.tokens import ensure_allowance, get_token_balance
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction

VARIABLE_RATE_MODE = 2
STABLE_RATE_MODE = 1
REFERRAL_CODE = 0


class SparkLendAdapter(AaveV3Adapter):
    adapter_type = "SPARKLEND"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        sign_callback=None,
        wallet_address: str | None = None,
    ) -> None:
        # SparkLend is Aave v3-based. We inherit common pool interactions from
        # AaveV3Adapter and keep Spark-specific reads/rate modes here.
        super().__init__(
            config=config, sign_callback=sign_callback, wallet_address=wallet_address
        )
        self.name = "sparklend_adapter"

        # Cache: (chain_id, underlying.lower()) -> reserve config dict
        self._reserve_config_by_chain_underlying: dict[
            tuple[int, str], dict[str, Any]
        ] = {}

    def _parse_reserve_config(
        self, data: tuple[Any, ...], chain_id: int, underlying: str
    ) -> dict[str, Any]:
        cfg = {
            "decimals": data[0],
            "ltv_bps": data[1],
            "liquidation_threshold_bps": data[2],
            "liquidation_bonus_bps": data[3],
            "reserve_factor_bps": data[4],
            "usage_as_collateral_enabled": data[5],
            "borrowing_enabled": data[6],
            "stable_borrow_rate_enabled": data[7],
            "is_active": data[8],
            "is_frozen": data[9],
        }
        self._reserve_config_by_chain_underlying[
            (chain_id, to_checksum_address(underlying).lower())
        ] = cfg
        return cfg

    def _entry(self, chain_id: int) -> dict[str, str]:
        entry = SPARKLEND_BY_CHAIN.get(int(chain_id))
        if not entry:
            raise ValueError(f"Unsupported SparkLend chain_id={chain_id}")
        return entry

    async def _reserve_tokens(
        self, *, chain_id: int, underlying: str
    ) -> tuple[str, str, str]:
        underlying = to_checksum_address(underlying)
        entry = self._entry(chain_id)
        data_provider_addr = entry.get("protocol_data_provider")
        if not data_provider_addr:
            raise ValueError(
                f"protocol_data_provider not configured for chain_id={chain_id}"
            )

        async with web3_utils.web3_from_chain_id(chain_id) as web3:
            dp = web3.eth.contract(
                address=to_checksum_address(data_provider_addr),
                abi=PROTOCOL_DATA_PROVIDER_ABI,
            )
            (
                a_token,
                stable_debt,
                variable_debt,
            ) = await dp.functions.getReserveTokensAddresses(underlying).call(
                block_identifier="pending"
            )

        return (
            to_checksum_address(a_token),
            to_checksum_address(stable_debt),
            to_checksum_address(variable_debt),
        )

    async def _reserve_config(
        self, *, chain_id: int, underlying: str, web3: Any
    ) -> dict[str, Any]:
        underlying = to_checksum_address(underlying)
        cache_key = (chain_id, underlying.lower())
        if cached := self._reserve_config_by_chain_underlying.get(cache_key):
            return cached

        entry = self._entry(chain_id)
        data_provider_addr = entry.get("protocol_data_provider")
        if not data_provider_addr:
            raise ValueError(
                f"protocol_data_provider not configured for chain_id={chain_id}"
            )

        dp = web3.eth.contract(
            address=to_checksum_address(data_provider_addr),
            abi=PROTOCOL_DATA_PROVIDER_ABI,
        )
        raw = await dp.functions.getReserveConfigurationData(underlying).call(
            block_identifier="pending"
        )
        cfg = self._parse_reserve_config(raw, chain_id, underlying)
        return cfg

    # ------------------
    # Write / tx methods
    # ------------------

    @require_wallet
    async def borrow(
        self,
        *,
        chain_id: int,
        asset: str,
        amount: int,
        rate_mode: int = VARIABLE_RATE_MODE,
    ) -> tuple[bool, Any]:
        if rate_mode not in (STABLE_RATE_MODE, VARIABLE_RATE_MODE):
            return False, "rate_mode must be 1 (stable) or 2 (variable)"

        try:
            if rate_mode == VARIABLE_RATE_MODE:
                return await super().borrow(
                    underlying_token=str(asset),
                    qty=int(amount),
                    chain_id=int(chain_id),
                    native=False,
                )

            if amount <= 0:
                return False, "amount must be positive"

            # Stable rate: AaveV3Adapter only supports variable rate, so call Pool directly.
            entry = self._entry(chain_id)
            pool = entry["pool"]
            asset = to_checksum_address(asset)

            async with web3_utils.web3_from_chain_id(chain_id) as w3:
                cfg = await self._reserve_config(
                    chain_id=chain_id, underlying=asset, web3=w3
                )
            if not cfg.get("stable_borrow_rate_enabled"):
                return (
                    False,
                    "stable borrow is not enabled for this reserve (use rate_mode=2)",
                )

            tx = await encode_call(
                target=pool,
                abi=POOL_ABI,
                fn_name="borrow",
                args=[asset, amount, rate_mode, REFERRAL_CODE, self.wallet_address],
                from_address=self.wallet_address,
                chain_id=chain_id,
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def repay(
        self,
        *,
        chain_id: int,
        asset: str,
        amount: int,
        rate_mode: int = VARIABLE_RATE_MODE,
        repay_full: bool = False,
    ) -> tuple[bool, Any]:
        if amount <= 0 and not repay_full:
            return False, "amount must be positive"

        if rate_mode not in (STABLE_RATE_MODE, VARIABLE_RATE_MODE):
            return False, "rate_mode must be 1 (stable) or 2 (variable)"

        try:
            if rate_mode == VARIABLE_RATE_MODE:
                return await super().repay(
                    underlying_token=str(asset),
                    qty=int(amount),
                    chain_id=int(chain_id),
                    native=False,
                    repay_full=bool(repay_full),
                )

            # Stable rate: AaveV3Adapter only supports variable rate, so call Pool directly.
            entry = self._entry(chain_id)
            pool = entry["pool"]
            asset = to_checksum_address(asset)

            repay_amount = MAX_UINT256 if repay_full else amount
            allowance_target = MAX_UINT256 if repay_full else amount

            approved = await ensure_allowance(
                token_address=asset,
                owner=self.wallet_address,
                spender=pool,
                amount=allowance_target,
                chain_id=chain_id,
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved[0]:
                return approved

            tx = await encode_call(
                target=pool,
                abi=POOL_ABI,
                fn_name="repay",
                args=[asset, repay_amount, rate_mode, self.wallet_address],
                from_address=self.wallet_address,
                chain_id=chain_id,
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def claim_rewards(self, *, chain_id: int) -> tuple[bool, Any]:
        try:
            entry = self._entry(chain_id)
            rewards_controller = entry.get("rewards_controller")
            data_provider_addr = entry.get("protocol_data_provider")
            if not rewards_controller:
                raise ValueError(
                    f"rewards_controller not configured for chain_id={chain_id}"
                )
            if not data_provider_addr:
                raise ValueError(
                    f"protocol_data_provider not configured for chain_id={chain_id}"
                )

            async with web3_utils.web3_from_chain_id(chain_id) as web3:
                dp = web3.eth.contract(
                    address=to_checksum_address(data_provider_addr),
                    abi=PROTOCOL_DATA_PROVIDER_ABI,
                )
                rewards = web3.eth.contract(
                    address=to_checksum_address(rewards_controller),
                    abi=REWARDS_CONTROLLER_ABI,
                )
                reserves = await dp.functions.getAllReservesTokens().call(
                    block_identifier="pending"
                )

                token_results = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=chain_id,
                    calls=[
                        Call(
                            dp,
                            "getReserveTokensAddresses",
                            (to_checksum_address(row[1]),),
                        )
                        for row in (reserves or [])
                    ],
                    block_identifier="pending",
                )

                token_candidates: set[str] = set()
                for token_addrs in token_results:
                    token_candidates.update(
                        cs
                        for addr in token_addrs
                        if (cs := to_checksum_address(addr)) != ZERO_ADDRESS
                    )

                reward_results = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=chain_id,
                    calls=[
                        Call(rewards, "getRewardsByAsset", (token,))
                        for token in token_candidates
                    ],
                    block_identifier="pending",
                )

                assets_set: set[str] = set()
                for token, rewards_for_asset in zip(
                    token_candidates, reward_results, strict=True
                ):
                    if rewards_for_asset:
                        assets_set.add(token)

            assets = sorted(assets_set)
            if not assets:
                return True, {"claimed": [], "note": "no incentivized assets found"}

            tx = await encode_call(
                target=rewards_controller,
                abi=REWARDS_CONTROLLER_ABI,
                fn_name="claimAllRewardsToSelf",
                args=[assets],
                from_address=self.wallet_address,
                chain_id=chain_id,
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:
            return False, str(exc)

    # -----------------
    # Read-only methods
    # -----------------

    async def get_all_markets(
        self, *, chain_id: int, include_caps: bool = True
    ) -> tuple[bool, list[dict[str, Any]] | str]:
        try:
            entry = self._entry(chain_id)
            data_provider_addr = entry.get("protocol_data_provider")
            if not data_provider_addr:
                raise ValueError(
                    f"protocol_data_provider not configured for chain_id={chain_id}"
                )

            async with web3_utils.web3_from_chain_id(chain_id) as web3:
                dp = web3.eth.contract(
                    address=to_checksum_address(data_provider_addr),
                    abi=PROTOCOL_DATA_PROVIDER_ABI,
                )
                reserves = await dp.functions.getAllReservesTokens().call(
                    block_identifier="pending"
                )

                # Build all per-reserve calls in one batch
                parsed_reserves = [
                    (str(row[0] or ""), to_checksum_address(row[1]))
                    for row in (reserves or [])
                ]
                calls: list[Call] = []
                # Per reserve: config, data, tokens (+ caps if requested)
                calls_per_reserve = 4 if include_caps else 3
                for _, underlying in parsed_reserves:
                    calls.append(Call(dp, "getReserveConfigurationData", (underlying,)))
                    calls.append(Call(dp, "getReserveData", (underlying,)))
                    calls.append(Call(dp, "getReserveTokensAddresses", (underlying,)))
                    if include_caps:
                        calls.append(Call(dp, "getReserveCaps", (underlying,)))

                results = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=chain_id,
                    calls=calls,
                    block_identifier="pending",
                )

                markets: list[dict[str, Any]] = []
                for i, (symbol, underlying) in enumerate(parsed_reserves):
                    base = i * calls_per_reserve
                    cfg_data = results[base]
                    reserve_data = results[base + 1]
                    token_addrs = results[base + 2]

                    cfg = self._parse_reserve_config(cfg_data, chain_id, underlying)

                    borrow_cap = None
                    supply_cap = None
                    if include_caps:
                        caps = results[base + 3]
                        borrow_cap = caps[0]
                        supply_cap = caps[1]

                    # (unbacked, accruedToTreasuryScaled, totalAToken, totalStableDebt, totalVariableDebt,
                    #  liquidityRate, variableBorrowRate, stableBorrowRate, averageStableBorrowRate,
                    #  liquidityIndex, variableBorrowIndex, lastUpdateTimestamp)
                    total_a_token = reserve_data[2]
                    total_stable_debt = reserve_data[3]
                    total_variable_debt = reserve_data[4]
                    liquidity_rate = reserve_data[5]
                    variable_borrow_rate = reserve_data[6]
                    stable_borrow_rate = reserve_data[7]

                    a_token = to_checksum_address(token_addrs[0])
                    stable_debt_token = to_checksum_address(token_addrs[1])
                    variable_debt_token = to_checksum_address(token_addrs[2])
                    decimals = cfg["decimals"]
                    unit = 10**decimals

                    supply_apr = ray_to_apr(liquidity_rate)
                    variable_borrow_apr = ray_to_apr(variable_borrow_rate)
                    stable_borrow_apr = ray_to_apr(stable_borrow_rate)

                    supply_apy = apr_to_apy(supply_apr)
                    variable_borrow_apy = apr_to_apy(variable_borrow_apr)
                    stable_borrow_apy = apr_to_apy(stable_borrow_apr)

                    supply_cap_headroom = None
                    if supply_cap and supply_cap > 0:
                        supply_cap_wei = supply_cap * unit
                        supply_cap_headroom = max(0, supply_cap_wei - total_a_token)

                    borrow_cap_headroom = None
                    if borrow_cap and borrow_cap > 0:
                        borrow_cap_wei = borrow_cap * unit
                        total_debt = total_stable_debt + total_variable_debt
                        borrow_cap_headroom = max(0, borrow_cap_wei - total_debt)

                    markets.append(
                        {
                            "chain_id": chain_id,
                            "pool": entry.get("pool"),
                            "underlying": underlying,
                            "symbol": symbol,
                            "decimals": decimals,
                            "supply_token": a_token,
                            "stable_debt_token": stable_debt_token,
                            "variable_debt_token": variable_debt_token,
                            "ltv_bps": cfg["ltv_bps"],
                            "liquidation_threshold_bps": cfg[
                                "liquidation_threshold_bps"
                            ],
                            "liquidation_bonus_bps": cfg["liquidation_bonus_bps"],
                            "reserve_factor_bps": cfg["reserve_factor_bps"],
                            "usage_as_collateral_enabled": cfg[
                                "usage_as_collateral_enabled"
                            ],
                            "borrowing_enabled": cfg["borrowing_enabled"],
                            "stable_borrow_enabled": cfg["stable_borrow_rate_enabled"],
                            "is_active": cfg["is_active"],
                            "is_frozen": cfg["is_frozen"],
                            "supply_cap": supply_cap,
                            "borrow_cap": borrow_cap,
                            "supply_cap_headroom": supply_cap_headroom,
                            "borrow_cap_headroom": borrow_cap_headroom,
                            "total_supply_raw": total_a_token,
                            "total_stable_debt_raw": total_stable_debt,
                            "total_variable_debt_raw": total_variable_debt,
                            "supply_apr": supply_apr,
                            "supply_apy": supply_apy,
                            "variable_borrow_apr": variable_borrow_apr,
                            "variable_borrow_apy": variable_borrow_apy,
                            "stable_borrow_apr": stable_borrow_apr,
                            "stable_borrow_apy": stable_borrow_apy,
                        }
                    )

                return True, markets
        except Exception as exc:
            return False, str(exc)

    async def get_pos(
        self, *, chain_id: int, asset: str, account: str | None = None
    ) -> tuple[bool, dict[str, Any] | str]:
        acct = account or self.wallet_address
        if not acct:
            return False, "strategy wallet address not configured"

        try:
            acct = to_checksum_address(acct)
            entry = self._entry(chain_id)
            data_provider_addr = entry.get("protocol_data_provider")
            if not data_provider_addr:
                raise ValueError(
                    f"protocol_data_provider not configured for chain_id={chain_id}"
                )

            underlying = to_checksum_address(asset)
            async with web3_utils.web3_from_chain_id(chain_id) as web3:
                dp = web3.eth.contract(
                    address=to_checksum_address(data_provider_addr),
                    abi=PROTOCOL_DATA_PROVIDER_ABI,
                )

                (
                    user_data,
                    token_addrs,
                    cfg_data,
                ) = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=chain_id,
                    calls=[
                        Call(dp, "getUserReserveData", (underlying, acct)),
                        Call(dp, "getReserveTokensAddresses", (underlying,)),
                        Call(dp, "getReserveConfigurationData", (underlying,)),
                    ],
                    block_identifier="pending",
                )

                (
                    current_a_token_balance,
                    current_stable_debt,
                    current_variable_debt,
                    principal_stable_debt,
                    scaled_variable_debt,
                    stable_borrow_rate,
                    liquidity_rate,
                    stable_rate_last_updated,
                    usage_as_collateral_enabled_on_user,
                ) = user_data

                (a_token, stable_debt_token, variable_debt_token) = token_addrs

                cfg = self._parse_reserve_config(cfg_data, chain_id, underlying)

            return True, {
                "protocol": "sparklend",
                "chain_id": chain_id,
                "pool": entry.get("pool"),
                "account": acct,
                "underlying": underlying,
                "decimals": cfg.get("decimals", 18),
                "supply_token": to_checksum_address(a_token),
                "stable_debt_token": to_checksum_address(stable_debt_token),
                "variable_debt_token": to_checksum_address(variable_debt_token),
                "supply_raw": current_a_token_balance,
                "stable_borrow_raw": current_stable_debt,
                "variable_borrow_raw": current_variable_debt,
                "principal_stable_debt_raw": principal_stable_debt,
                "scaled_variable_debt_raw": scaled_variable_debt,
                "stable_borrow_rate_ray": stable_borrow_rate,
                "liquidity_rate_ray": liquidity_rate,
                "stable_rate_last_updated": stable_rate_last_updated,
                "usage_as_collateral_enabled_on_user": usage_as_collateral_enabled_on_user,
                "reserve_config": cfg,
            }
        except Exception as exc:
            return False, str(exc)

    async def get_full_user_state(
        self,
        *,
        chain_id: int,
        account: str,
        include_zero_positions: bool = False,
    ) -> tuple[bool, dict[str, Any] | str]:
        try:
            entry = self._entry(chain_id)
            pool_addr = entry.get("pool")
            data_provider_addr = entry.get("protocol_data_provider")
            if not pool_addr:
                raise ValueError(f"pool not configured for chain_id={chain_id}")
            if not data_provider_addr:
                raise ValueError(
                    f"protocol_data_provider not configured for chain_id={chain_id}"
                )

            acct = to_checksum_address(account)
            async with web3_utils.web3_from_chain_id(chain_id) as web3:
                pool = web3.eth.contract(
                    address=to_checksum_address(pool_addr), abi=POOL_ABI
                )
                dp = web3.eth.contract(
                    address=to_checksum_address(data_provider_addr),
                    abi=PROTOCOL_DATA_PROVIDER_ABI,
                )

                (
                    account_data_tuple,
                    reserves,
                ) = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=chain_id,
                    calls=[
                        Call(pool, "getUserAccountData", (acct,)),
                        Call(dp, "getAllReservesTokens", ()),
                    ],
                    block_identifier="pending",
                )

                # Batch all per-reserve calls: userData + tokenAddrs + config
                parsed_reserves = [
                    (str(row[0] or ""), to_checksum_address(row[1]))
                    for row in (reserves or [])
                ]
                per_reserve_calls: list[Call] = []
                for _, underlying in parsed_reserves:
                    per_reserve_calls.append(
                        Call(dp, "getUserReserveData", (underlying, acct))
                    )
                    per_reserve_calls.append(
                        Call(dp, "getReserveTokensAddresses", (underlying,))
                    )
                    per_reserve_calls.append(
                        Call(dp, "getReserveConfigurationData", (underlying,))
                    )

                per_reserve_results = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=chain_id,
                    calls=per_reserve_calls,
                    block_identifier="pending",
                )

                positions: list[dict[str, Any]] = []
                for i, (symbol, underlying) in enumerate(parsed_reserves):
                    base = i * 3
                    user_data = per_reserve_results[base]
                    token_addrs = per_reserve_results[base + 1]
                    cfg_data = per_reserve_results[base + 2]

                    supply = user_data[0]
                    stable_debt = user_data[1]
                    variable_debt = user_data[2]
                    collateral_enabled = user_data[8]

                    if (
                        not include_zero_positions
                        and supply <= 0
                        and stable_debt <= 0
                        and variable_debt <= 0
                        and not collateral_enabled
                    ):
                        continue

                    cfg = self._parse_reserve_config(cfg_data, chain_id, underlying)

                    positions.append(
                        {
                            "underlying": underlying,
                            "symbol": symbol,
                            "decimals": cfg.get("decimals", 18),
                            "supply_token": to_checksum_address(token_addrs[0]),
                            "stable_debt_token": to_checksum_address(token_addrs[1]),
                            "variable_debt_token": to_checksum_address(token_addrs[2]),
                            "supply_raw": supply,
                            "stable_borrow_raw": stable_debt,
                            "variable_borrow_raw": variable_debt,
                            "usage_as_collateral_enabled_on_user": collateral_enabled,
                            "reserve_config": cfg,
                        }
                    )

            account_data = {
                "total_collateral_base": account_data_tuple[0],
                "total_debt_base": account_data_tuple[1],
                "available_borrows_base": account_data_tuple[2],
                "current_liquidation_threshold": account_data_tuple[3],
                "ltv": account_data_tuple[4],
                "health_factor": account_data_tuple[5],
            }

            return True, {
                "protocol": "sparklend",
                "chain_id": chain_id,
                "pool": entry.get("pool"),
                "account": acct,
                "account_data": account_data,
                "positions": positions,
            }
        except Exception as exc:
            return False, str(exc)

    # -----------------------
    # Optional native helpers
    # -----------------------

    @require_wallet
    async def lend_native(self, *, chain_id: int, amount: int) -> tuple[bool, Any]:
        if amount <= 0:
            return False, "amount must be positive"
        wrapped = await self._wrapped_native(chain_id=chain_id)
        return await self.lend(
            underlying_token=wrapped, qty=amount, chain_id=chain_id, native=True
        )

    @require_wallet
    async def unlend_native(
        self, *, chain_id: int, amount: int, withdraw_full: bool = False
    ) -> tuple[bool, Any]:
        if amount <= 0 and not withdraw_full:
            return False, "amount must be positive"
        wrapped = await self._wrapped_native(chain_id=chain_id)
        return await self.unlend(
            underlying_token=wrapped,
            qty=amount,
            chain_id=chain_id,
            native=True,
            withdraw_full=withdraw_full,
        )

    @require_wallet
    async def borrow_native(
        self,
        *,
        chain_id: int,
        amount: int,
        rate_mode: int = VARIABLE_RATE_MODE,
    ) -> tuple[bool, Any]:
        if amount <= 0:
            return False, "amount must be positive"

        if rate_mode not in (STABLE_RATE_MODE, VARIABLE_RATE_MODE):
            return False, "rate_mode must be 1 (stable) or 2 (variable)"

        try:
            entry = self._entry(chain_id)
            pool = entry.get("pool")
            gateway = entry.get("wrapped_native_gateway")
            if not pool:
                raise ValueError(f"pool not configured for chain_id={chain_id}")
            if not gateway:
                raise ValueError(
                    f"wrapped_native_gateway not configured for chain_id={chain_id}"
                )

            if rate_mode == STABLE_RATE_MODE:
                wrapped = await self._wrapped_native(chain_id=chain_id)
                async with web3_utils.web3_from_chain_id(chain_id) as w3:
                    cfg = await self._reserve_config(
                        chain_id=chain_id, underlying=wrapped, web3=w3
                    )
                if not cfg.get("stable_borrow_rate_enabled"):
                    return (
                        False,
                        "stable borrow is not enabled for wrapped native (use rate_mode=2)",
                    )

            tx = await encode_call(
                target=gateway,
                abi=WETH_GATEWAY_ABI,
                fn_name="borrowETH",
                args=[to_checksum_address(pool), amount, rate_mode, REFERRAL_CODE],
                from_address=self.wallet_address,
                chain_id=chain_id,
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def repay_native(
        self,
        *,
        chain_id: int,
        amount: int,
        rate_mode: int = VARIABLE_RATE_MODE,
        repay_full: bool = False,
    ) -> tuple[bool, Any]:
        if amount <= 0 and not repay_full:
            return False, "amount must be positive"

        if rate_mode not in (STABLE_RATE_MODE, VARIABLE_RATE_MODE):
            return False, "rate_mode must be 1 (stable) or 2 (variable)"

        try:
            entry = self._entry(chain_id)
            pool = entry.get("pool")
            gateway = entry.get("wrapped_native_gateway")
            if not pool:
                raise ValueError(f"pool not configured for chain_id={chain_id}")
            if not gateway:
                raise ValueError(
                    f"wrapped_native_gateway not configured for chain_id={chain_id}"
                )

            repay_amount = MAX_UINT256 if repay_full else amount

            if repay_full:
                wrapped = await self._wrapped_native(chain_id=chain_id)
                _, stable_debt, variable_debt = await self._reserve_tokens(
                    chain_id=chain_id, underlying=wrapped
                )
                debt_token = (
                    variable_debt if rate_mode == VARIABLE_RATE_MODE else stable_debt
                )
                if debt_token.lower() == ZERO_ADDRESS:
                    return False, "debt token address not found for wrapped native"

                debt, native_balance = await asyncio.gather(
                    get_token_balance(
                        debt_token,
                        chain_id,
                        self.wallet_address,
                        block_identifier="pending",
                    ),
                    get_token_balance(
                        None,
                        chain_id,
                        self.wallet_address,
                        block_identifier="pending",
                    ),
                )
                if debt <= 0:
                    return True, None
                buffer_wei = max(1, debt // 10_000)  # 0.01%
                value = debt + buffer_wei
                if native_balance < value:
                    if native_balance < debt:
                        return (
                            False,
                            f"insufficient native balance for repay_full (debt_wei={debt}, balance_wei={native_balance})",
                        )
                    value = debt
            else:
                value = amount

            tx = await encode_call(
                target=gateway,
                abi=WETH_GATEWAY_ABI,
                fn_name="repayETH",
                args=[
                    to_checksum_address(pool),
                    repay_amount,
                    rate_mode,
                    self.wallet_address,
                ],
                from_address=self.wallet_address,
                chain_id=chain_id,
                value=value,
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:
            return False, str(exc)
