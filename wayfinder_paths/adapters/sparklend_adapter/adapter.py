from __future__ import annotations

from typing import Any

from eth_utils import to_checksum_address

from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter, require_wallet
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
from wayfinder_paths.core.utils.tokens import ensure_allowance, get_token_balance
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction

VARIABLE_RATE_MODE = 2
STABLE_RATE_MODE = 1
REFERRAL_CODE = 0


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


class SparkLendAdapter(BaseAdapter):
    adapter_type = "SPARKLEND"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        sign_callback=None,
        wallet_address: str | None = None,
    ) -> None:
        super().__init__("sparklend_adapter", config or {})
        self.sign_callback = sign_callback

        self.wallet_address: str | None = (
            to_checksum_address(wallet_address) if wallet_address else None
        )

        # Cache: chain_id -> wrapped native token address (WETH/WXDAI)
        self._wrapped_native_by_chain: dict[int, str] = {}
        # Cache: (chain_id, underlying.lower()) -> (aToken, stableDebtToken, variableDebtToken)
        self._reserve_tokens_by_chain_underlying: dict[
            tuple[int, str], tuple[str, str, str]
        ] = {}
        # Cache: (chain_id, underlying.lower()) -> reserve config dict
        self._reserve_config_by_chain_underlying: dict[tuple[int, str], dict[str, Any]] = (
            {}
        )

    def _entry(self, chain_id: int) -> dict[str, str]:
        entry = SPARKLEND_BY_CHAIN.get(int(chain_id))
        if not entry:
            raise ValueError(f"Unsupported SparkLend chain_id={chain_id}")
        return entry

    async def _wrapped_native(self, *, chain_id: int) -> str:
        cid = int(chain_id)
        cached = self._wrapped_native_by_chain.get(cid)
        if cached:
            return cached

        entry = self._entry(cid)
        gateway = entry.get("wrapped_native_gateway")
        if not gateway:
            raise ValueError(f"wrapped_native_gateway not configured for chain_id={cid}")

        async with web3_utils.web3_from_chain_id(cid) as web3:
            gw = web3.eth.contract(address=gateway, abi=WETH_GATEWAY_ABI)
            wrapped = await gw.functions.getWETHAddress().call(
                block_identifier="pending"
            )
            wrapped = to_checksum_address(str(wrapped))
            self._wrapped_native_by_chain[cid] = wrapped
            return wrapped

    async def _reserve_tokens(
        self, *, chain_id: int, underlying: str
    ) -> tuple[str, str, str]:
        cid = int(chain_id)
        underlying = to_checksum_address(underlying)
        cache_key = (cid, underlying.lower())
        cached = self._reserve_tokens_by_chain_underlying.get(cache_key)
        if cached:
            return cached

        entry = self._entry(cid)
        data_provider_addr = entry.get("protocol_data_provider")
        if not data_provider_addr:
            raise ValueError(f"protocol_data_provider not configured for chain_id={cid}")

        async with web3_utils.web3_from_chain_id(cid) as web3:
            dp = web3.eth.contract(
                address=to_checksum_address(data_provider_addr),
                abi=PROTOCOL_DATA_PROVIDER_ABI,
            )
            a_token, stable_debt, variable_debt = await dp.functions.getReserveTokensAddresses(
                underlying
            ).call(
                block_identifier="pending"
            )

        tokens = (
            to_checksum_address(str(a_token)),
            to_checksum_address(str(stable_debt)),
            to_checksum_address(str(variable_debt)),
        )
        self._reserve_tokens_by_chain_underlying[cache_key] = tokens
        return tokens

    async def _reserve_config(
        self, *, chain_id: int, underlying: str, web3: Any | None = None
    ) -> dict[str, Any]:
        cid = int(chain_id)
        underlying = to_checksum_address(underlying)
        cache_key = (cid, underlying.lower())
        if cached := self._reserve_config_by_chain_underlying.get(cache_key):
            return cached

        entry = self._entry(cid)
        data_provider_addr = entry.get("protocol_data_provider")
        if not data_provider_addr:
            raise ValueError(f"protocol_data_provider not configured for chain_id={cid}")

        async def _read(w3: Any) -> dict[str, Any]:
            dp = w3.eth.contract(
                address=to_checksum_address(data_provider_addr),
                abi=PROTOCOL_DATA_PROVIDER_ABI,
            )
            (
                decimals,
                ltv,
                liq_threshold,
                liq_bonus,
                reserve_factor,
                usage_as_collateral_enabled,
                borrowing_enabled,
                stable_borrow_rate_enabled,
                is_active,
                is_frozen,
            ) = await dp.functions.getReserveConfigurationData(underlying).call(
                block_identifier="pending"
            )

            cfg = {
                "decimals": _as_int(decimals, 18),
                "ltv_bps": _as_int(ltv),
                "liquidation_threshold_bps": _as_int(liq_threshold),
                "liquidation_bonus_bps": _as_int(liq_bonus),
                "reserve_factor_bps": _as_int(reserve_factor),
                "usage_as_collateral_enabled": bool(usage_as_collateral_enabled),
                "borrowing_enabled": bool(borrowing_enabled),
                "stable_borrow_rate_enabled": bool(stable_borrow_rate_enabled),
                "is_active": bool(is_active),
                "is_frozen": bool(is_frozen),
            }
            self._reserve_config_by_chain_underlying[cache_key] = cfg
            return cfg

        if web3 is not None:
            return await _read(web3)

        async with web3_utils.web3_from_chain_id(cid) as w3:
            return await _read(w3)

    # ------------------
    # Write / tx methods
    # ------------------

    @require_wallet
    async def lend(self, *, chain_id: int, asset: str, amount: int) -> tuple[bool, Any]:
        strategy = self.wallet_address
        amount = int(amount)
        if amount <= 0:
            return False, "amount must be positive"

        try:
            entry = self._entry(int(chain_id))
            pool = entry["pool"]
            asset = to_checksum_address(asset)

            approved = await ensure_allowance(
                token_address=asset,
                owner=strategy,
                spender=pool,
                amount=amount,
                chain_id=int(chain_id),
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved[0]:
                return approved

            tx = await encode_call(
                target=pool,
                abi=POOL_ABI,
                fn_name="supply",
                args=[asset, int(amount), strategy, REFERRAL_CODE],
                from_address=strategy,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    @require_wallet
    async def unlend(
        self,
        *,
        chain_id: int,
        asset: str,
        amount: int,
        withdraw_full: bool = False,
    ) -> tuple[bool, Any]:
        strategy = self.wallet_address
        amount = int(amount)
        if amount <= 0 and not withdraw_full:
            return False, "amount must be positive"

        try:
            entry = self._entry(int(chain_id))
            pool = entry["pool"]
            asset = to_checksum_address(asset)
            withdraw_amount = MAX_UINT256 if withdraw_full else int(amount)

            tx = await encode_call(
                target=pool,
                abi=POOL_ABI,
                fn_name="withdraw",
                args=[asset, int(withdraw_amount), strategy],
                from_address=strategy,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    @require_wallet
    async def borrow(
        self,
        *,
        chain_id: int,
        asset: str,
        amount: int,
        rate_mode: int = VARIABLE_RATE_MODE,
    ) -> tuple[bool, Any]:
        strategy = self.wallet_address
        amount = int(amount)
        if amount <= 0:
            return False, "amount must be positive"

        rate_mode = int(rate_mode)
        if rate_mode not in (STABLE_RATE_MODE, VARIABLE_RATE_MODE):
            return False, "rate_mode must be 1 (stable) or 2 (variable)"

        try:
            entry = self._entry(int(chain_id))
            pool = entry["pool"]
            asset = to_checksum_address(asset)

            if rate_mode == STABLE_RATE_MODE:
                cfg = await self._reserve_config(chain_id=int(chain_id), underlying=asset)
                if not cfg.get("stable_borrow_rate_enabled"):
                    return (
                        False,
                        "stable borrow is not enabled for this reserve (use rate_mode=2)",
                    )

            tx = await encode_call(
                target=pool,
                abi=POOL_ABI,
                fn_name="borrow",
                args=[asset, int(amount), int(rate_mode), REFERRAL_CODE, strategy],
                from_address=strategy,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:  # noqa: BLE001
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
        strategy = self.wallet_address
        amount = int(amount)
        if amount <= 0 and not repay_full:
            return False, "amount must be positive"

        rate_mode = int(rate_mode)
        if rate_mode not in (STABLE_RATE_MODE, VARIABLE_RATE_MODE):
            return False, "rate_mode must be 1 (stable) or 2 (variable)"

        try:
            entry = self._entry(int(chain_id))
            pool = entry["pool"]
            asset = to_checksum_address(asset)

            repay_amount = MAX_UINT256 if repay_full else int(amount)
            allowance_target = MAX_UINT256 if repay_full else int(amount)

            approved = await ensure_allowance(
                token_address=asset,
                owner=strategy,
                spender=pool,
                amount=int(allowance_target),
                chain_id=int(chain_id),
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved[0]:
                return approved

            tx = await encode_call(
                target=pool,
                abi=POOL_ABI,
                fn_name="repay",
                args=[asset, int(repay_amount), int(rate_mode), strategy],
                from_address=strategy,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    @require_wallet
    async def set_collateral(
        self, *, chain_id: int, asset: str, enabled: bool
    ) -> tuple[bool, Any]:
        strategy = self.wallet_address
        try:
            entry = self._entry(int(chain_id))
            pool = entry["pool"]
            asset = to_checksum_address(asset)

            tx = await encode_call(
                target=pool,
                abi=POOL_ABI,
                fn_name="setUserUseReserveAsCollateral",
                args=[asset, bool(enabled)],
                from_address=strategy,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    @require_wallet
    async def claim_rewards(self, *, chain_id: int) -> tuple[bool, Any]:
        strategy = self.wallet_address
        try:
            entry = self._entry(int(chain_id))
            rewards_controller = entry.get("rewards_controller")
            data_provider_addr = entry.get("protocol_data_provider")
            if not rewards_controller:
                raise ValueError(f"rewards_controller not configured for chain_id={chain_id}")
            if not data_provider_addr:
                raise ValueError(
                    f"protocol_data_provider not configured for chain_id={chain_id}"
                )

            async with web3_utils.web3_from_chain_id(int(chain_id)) as web3:
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

                token_candidates: set[str] = set()
                for row in reserves or []:
                    try:
                        underlying = to_checksum_address(str(row[1]))
                    except Exception:  # noqa: BLE001
                        continue
                    try:
                        a_token, stable_debt, variable_debt = (
                            await dp.functions.getReserveTokensAddresses(underlying).call(
                                block_identifier="pending"
                            )
                        )
                    except Exception:  # noqa: BLE001
                        continue

                    for addr in (a_token, stable_debt, variable_debt):
                        addr_s = str(addr)
                        if addr_s and addr_s.strip().lower() != ZERO_ADDRESS:
                            token_candidates.add(to_checksum_address(addr_s))

                assets_set: set[str] = set()
                for token in token_candidates:
                    try:
                        rewards_for_asset = await rewards.functions.getRewardsByAsset(
                            to_checksum_address(token)
                        ).call(block_identifier="pending")
                    except Exception:  # noqa: BLE001
                        continue
                    if rewards_for_asset:
                        assets_set.add(to_checksum_address(token))

            assets = sorted(assets_set)
            if not assets:
                return True, {"claimed": [], "note": "no incentivized assets found"}

            tx = await encode_call(
                target=rewards_controller,
                abi=REWARDS_CONTROLLER_ABI,
                fn_name="claimAllRewardsToSelf",
                args=[[to_checksum_address(a) for a in assets]],
                from_address=strategy,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    # -----------------
    # Read-only methods
    # -----------------

    async def get_all_markets(
        self, *, chain_id: int, include_caps: bool = True
    ) -> tuple[bool, list[dict[str, Any]] | str]:
        try:
            entry = self._entry(int(chain_id))
            data_provider_addr = entry.get("protocol_data_provider")
            if not data_provider_addr:
                raise ValueError(
                    f"protocol_data_provider not configured for chain_id={chain_id}"
                )

            async with web3_utils.web3_from_chain_id(int(chain_id)) as web3:
                dp = web3.eth.contract(
                    address=to_checksum_address(data_provider_addr),
                    abi=PROTOCOL_DATA_PROVIDER_ABI,
                )
                reserves = await dp.functions.getAllReservesTokens().call(
                    block_identifier="pending"
                )

                markets: list[dict[str, Any]] = []
                for row in reserves or []:
                    symbol = str(row[0] or "")
                    underlying = to_checksum_address(str(row[1]))

                    (
                        decimals,
                        ltv,
                        liq_threshold,
                        liq_bonus,
                        reserve_factor,
                        usage_as_collateral_enabled,
                        borrowing_enabled,
                        stable_borrow_rate_enabled,
                        is_active,
                        is_frozen,
                    ) = await dp.functions.getReserveConfigurationData(underlying).call(
                        block_identifier="pending"
                    )

                    borrow_cap = None
                    supply_cap = None
                    if include_caps:
                        bc, sc = await dp.functions.getReserveCaps(underlying).call(
                            block_identifier="pending"
                        )
                        borrow_cap = _as_int(bc)
                        supply_cap = _as_int(sc)

                    reserve_data = await dp.functions.getReserveData(underlying).call(
                        block_identifier="pending"
                    )
                    # (unbacked, accruedToTreasuryScaled, totalAToken, totalStableDebt, totalVariableDebt,
                    #  liquidityRate, variableBorrowRate, stableBorrowRate, averageStableBorrowRate,
                    #  liquidityIndex, variableBorrowIndex, lastUpdateTimestamp)
                    total_a_token = _as_int(reserve_data[2] if len(reserve_data) > 2 else 0)
                    total_stable_debt = _as_int(
                        reserve_data[3] if len(reserve_data) > 3 else 0
                    )
                    total_variable_debt = _as_int(
                        reserve_data[4] if len(reserve_data) > 4 else 0
                    )
                    liquidity_rate = _as_int(
                        reserve_data[5] if len(reserve_data) > 5 else 0
                    )
                    variable_borrow_rate = _as_int(
                        reserve_data[6] if len(reserve_data) > 6 else 0
                    )
                    stable_borrow_rate = _as_int(
                        reserve_data[7] if len(reserve_data) > 7 else 0
                    )

                    a_token, stable_debt_token, variable_debt_token = (
                        await dp.functions.getReserveTokensAddresses(underlying).call(
                            block_identifier="pending"
                        )
                    )
                    a_token = to_checksum_address(str(a_token))
                    stable_debt_token = to_checksum_address(str(stable_debt_token))
                    variable_debt_token = to_checksum_address(str(variable_debt_token))
                    self._reserve_tokens_by_chain_underlying[
                        (int(chain_id), underlying.lower())
                    ] = (a_token, stable_debt_token, variable_debt_token)

                    decimals_i = _as_int(decimals, 18)
                    unit = 10 ** max(0, decimals_i)

                    supply_apr = float(ray_to_apr(liquidity_rate))
                    variable_borrow_apr = float(ray_to_apr(variable_borrow_rate))
                    stable_borrow_apr = float(ray_to_apr(stable_borrow_rate))

                    supply_apy = float(apr_to_apy(supply_apr))
                    variable_borrow_apy = float(apr_to_apy(variable_borrow_apr))
                    stable_borrow_apy = float(apr_to_apy(stable_borrow_apr))

                    supply_cap_headroom = None
                    if supply_cap and int(supply_cap) > 0:
                        supply_cap_wei = int(supply_cap) * unit
                        supply_cap_headroom = max(0, int(supply_cap_wei) - int(total_a_token))

                    borrow_cap_headroom = None
                    if borrow_cap and int(borrow_cap) > 0:
                        borrow_cap_wei = int(borrow_cap) * unit
                        total_debt = int(total_stable_debt) + int(total_variable_debt)
                        borrow_cap_headroom = max(0, int(borrow_cap_wei) - int(total_debt))

                    markets.append(
                        {
                            "chain_id": int(chain_id),
                            "pool": entry.get("pool"),
                            "underlying": underlying,
                            "symbol": symbol,
                            "decimals": int(decimals_i),
                            "supply_token": a_token,
                            "stable_debt_token": stable_debt_token,
                            "variable_debt_token": variable_debt_token,
                            "ltv_bps": _as_int(ltv),
                            "liquidation_threshold_bps": _as_int(liq_threshold),
                            "liquidation_bonus_bps": _as_int(liq_bonus),
                            "reserve_factor_bps": _as_int(reserve_factor),
                            "usage_as_collateral_enabled": bool(usage_as_collateral_enabled),
                            "borrowing_enabled": bool(borrowing_enabled),
                            "stable_borrow_enabled": bool(stable_borrow_rate_enabled),
                            "is_active": bool(is_active),
                            "is_frozen": bool(is_frozen),
                            "supply_cap": int(supply_cap) if supply_cap is not None else None,
                            "borrow_cap": int(borrow_cap) if borrow_cap is not None else None,
                            "supply_cap_headroom": supply_cap_headroom,
                            "borrow_cap_headroom": borrow_cap_headroom,
                            "total_supply_raw": int(total_a_token),
                            "total_stable_debt_raw": int(total_stable_debt),
                            "total_variable_debt_raw": int(total_variable_debt),
                            "supply_apr": float(supply_apr),
                            "supply_apy": float(supply_apy),
                            "variable_borrow_apr": float(variable_borrow_apr),
                            "variable_borrow_apy": float(variable_borrow_apy),
                            "stable_borrow_apr": float(stable_borrow_apr),
                            "stable_borrow_apy": float(stable_borrow_apy),
                        }
                    )

                return True, markets
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def get_pos(
        self, *, chain_id: int, asset: str, account: str | None = None
    ) -> tuple[bool, dict[str, Any] | str]:
        acct = account or self.wallet_address
        if not acct:
            return False, "strategy wallet address not configured"

        try:
            acct = to_checksum_address(acct)
            entry = self._entry(int(chain_id))
            data_provider_addr = entry.get("protocol_data_provider")
            if not data_provider_addr:
                raise ValueError(
                    f"protocol_data_provider not configured for chain_id={chain_id}"
                )

            underlying = to_checksum_address(asset)
            async with web3_utils.web3_from_chain_id(int(chain_id)) as web3:
                dp = web3.eth.contract(
                    address=to_checksum_address(data_provider_addr),
                    abi=PROTOCOL_DATA_PROVIDER_ABI,
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
                ) = await dp.functions.getUserReserveData(underlying, acct).call(
                    block_identifier="pending"
                )

                a_token, stable_debt_token, variable_debt_token = (
                    await dp.functions.getReserveTokensAddresses(underlying).call(
                        block_identifier="pending"
                    )
                )

                cfg = await self._reserve_config(
                    chain_id=int(chain_id), underlying=underlying, web3=web3
                )

            return True, {
                "protocol": "sparklend",
                "chain_id": int(chain_id),
                "pool": entry.get("pool"),
                "account": acct,
                "underlying": underlying,
                "decimals": int(cfg.get("decimals") or 18),
                "supply_token": to_checksum_address(str(a_token)),
                "stable_debt_token": to_checksum_address(str(stable_debt_token)),
                "variable_debt_token": to_checksum_address(str(variable_debt_token)),
                "supply_raw": _as_int(current_a_token_balance),
                "stable_borrow_raw": _as_int(current_stable_debt),
                "variable_borrow_raw": _as_int(current_variable_debt),
                "principal_stable_debt_raw": _as_int(principal_stable_debt),
                "scaled_variable_debt_raw": _as_int(scaled_variable_debt),
                "stable_borrow_rate_ray": _as_int(stable_borrow_rate),
                "liquidity_rate_ray": _as_int(liquidity_rate),
                "stable_rate_last_updated": _as_int(stable_rate_last_updated),
                "usage_as_collateral_enabled_on_user": bool(
                    usage_as_collateral_enabled_on_user
                ),
                "reserve_config": cfg,
            }
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def get_full_user_state(
        self,
        *,
        chain_id: int,
        account: str,
        include_zero_positions: bool = False,
    ) -> tuple[bool, dict[str, Any] | str]:
        try:
            entry = self._entry(int(chain_id))
            pool_addr = entry.get("pool")
            data_provider_addr = entry.get("protocol_data_provider")
            if not pool_addr:
                raise ValueError(f"pool not configured for chain_id={chain_id}")
            if not data_provider_addr:
                raise ValueError(
                    f"protocol_data_provider not configured for chain_id={chain_id}"
                )

            acct = to_checksum_address(account)
            async with web3_utils.web3_from_chain_id(int(chain_id)) as web3:
                pool = web3.eth.contract(address=to_checksum_address(pool_addr), abi=POOL_ABI)
                dp = web3.eth.contract(
                    address=to_checksum_address(data_provider_addr),
                    abi=PROTOCOL_DATA_PROVIDER_ABI,
                )

                account_data_tuple = await pool.functions.getUserAccountData(acct).call(
                    block_identifier="pending"
                )
                reserves = await dp.functions.getAllReservesTokens().call(
                    block_identifier="pending"
                )

                positions: list[dict[str, Any]] = []
                for row in reserves or []:
                    symbol = str(row[0] or "")
                    underlying = to_checksum_address(str(row[1]))

                    user_data = await dp.functions.getUserReserveData(
                        underlying, acct
                    ).call(block_identifier="pending")
                    supply = _as_int(user_data[0] if len(user_data) > 0 else 0)
                    stable_debt = _as_int(user_data[1] if len(user_data) > 1 else 0)
                    variable_debt = _as_int(user_data[2] if len(user_data) > 2 else 0)
                    collateral_enabled = bool(user_data[8] if len(user_data) > 8 else False)

                    if (
                        not include_zero_positions
                        and supply <= 0
                        and stable_debt <= 0
                        and variable_debt <= 0
                        and not collateral_enabled
                    ):
                        continue

                    a_token, stable_debt_token, variable_debt_token = (
                        await dp.functions.getReserveTokensAddresses(underlying).call(
                            block_identifier="pending"
                        )
                    )
                    cfg = await self._reserve_config(
                        chain_id=int(chain_id), underlying=underlying, web3=web3
                    )

                    positions.append(
                        {
                            "underlying": underlying,
                            "symbol": symbol,
                            "decimals": int(cfg.get("decimals") or 18),
                            "supply_token": to_checksum_address(str(a_token)),
                            "stable_debt_token": to_checksum_address(str(stable_debt_token)),
                            "variable_debt_token": to_checksum_address(
                                str(variable_debt_token)
                            ),
                            "supply_raw": supply,
                            "stable_borrow_raw": stable_debt,
                            "variable_borrow_raw": variable_debt,
                            "usage_as_collateral_enabled_on_user": collateral_enabled,
                            "reserve_config": cfg,
                        }
                    )

            account_data = {
                "total_collateral_base": _as_int(
                    account_data_tuple[0] if len(account_data_tuple) > 0 else 0
                ),
                "total_debt_base": _as_int(
                    account_data_tuple[1] if len(account_data_tuple) > 1 else 0
                ),
                "available_borrows_base": _as_int(
                    account_data_tuple[2] if len(account_data_tuple) > 2 else 0
                ),
                "current_liquidation_threshold": _as_int(
                    account_data_tuple[3] if len(account_data_tuple) > 3 else 0
                ),
                "ltv": _as_int(account_data_tuple[4] if len(account_data_tuple) > 4 else 0),
                "health_factor": _as_int(
                    account_data_tuple[5] if len(account_data_tuple) > 5 else 0
                ),
            }

            return True, {
                "protocol": "sparklend",
                "chain_id": int(chain_id),
                "pool": entry.get("pool"),
                "account": acct,
                "account_data": account_data,
                "positions": positions,
            }
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    # -----------------------
    # Optional native helpers
    # -----------------------

    @require_wallet
    async def supply_native(self, *, chain_id: int, amount: int) -> tuple[bool, Any]:
        strategy = self.wallet_address
        amount = int(amount)
        if amount <= 0:
            return False, "amount must be positive"

        try:
            entry = self._entry(int(chain_id))
            pool = entry.get("pool")
            gateway = entry.get("wrapped_native_gateway")
            if not pool:
                raise ValueError(f"pool not configured for chain_id={chain_id}")
            if not gateway:
                raise ValueError(
                    f"wrapped_native_gateway not configured for chain_id={chain_id}"
                )

            tx = await encode_call(
                target=gateway,
                abi=WETH_GATEWAY_ABI,
                fn_name="depositETH",
                args=[to_checksum_address(pool), strategy, REFERRAL_CODE],
                from_address=strategy,
                chain_id=int(chain_id),
                value=int(amount),
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    @require_wallet
    async def withdraw_native(
        self, *, chain_id: int, amount: int, withdraw_full: bool = False
    ) -> tuple[bool, Any]:
        strategy = self.wallet_address
        amount = int(amount)
        if amount <= 0 and not withdraw_full:
            return False, "amount must be positive"

        try:
            entry = self._entry(int(chain_id))
            pool = entry.get("pool")
            gateway = entry.get("wrapped_native_gateway")
            if not pool:
                raise ValueError(f"pool not configured for chain_id={chain_id}")
            if not gateway:
                raise ValueError(
                    f"wrapped_native_gateway not configured for chain_id={chain_id}"
                )

            wrapped = await self._wrapped_native(chain_id=int(chain_id))
            a_token, _, _ = await self._reserve_tokens(
                chain_id=int(chain_id), underlying=wrapped
            )

            allowance_target = MAX_UINT256 if withdraw_full else int(amount)
            approved = await ensure_allowance(
                token_address=a_token,
                owner=strategy,
                spender=gateway,
                amount=int(allowance_target),
                chain_id=int(chain_id),
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved[0]:
                return approved

            withdraw_amount = MAX_UINT256 if withdraw_full else int(amount)
            tx = await encode_call(
                target=gateway,
                abi=WETH_GATEWAY_ABI,
                fn_name="withdrawETH",
                args=[to_checksum_address(pool), int(withdraw_amount), strategy],
                from_address=strategy,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    @require_wallet
    async def borrow_native(
        self,
        *,
        chain_id: int,
        amount: int,
        rate_mode: int = VARIABLE_RATE_MODE,
    ) -> tuple[bool, Any]:
        strategy = self.wallet_address
        amount = int(amount)
        if amount <= 0:
            return False, "amount must be positive"

        rate_mode = int(rate_mode)
        if rate_mode not in (STABLE_RATE_MODE, VARIABLE_RATE_MODE):
            return False, "rate_mode must be 1 (stable) or 2 (variable)"

        try:
            entry = self._entry(int(chain_id))
            pool = entry.get("pool")
            gateway = entry.get("wrapped_native_gateway")
            if not pool:
                raise ValueError(f"pool not configured for chain_id={chain_id}")
            if not gateway:
                raise ValueError(
                    f"wrapped_native_gateway not configured for chain_id={chain_id}"
                )

            if rate_mode == STABLE_RATE_MODE:
                wrapped = await self._wrapped_native(chain_id=int(chain_id))
                cfg = await self._reserve_config(chain_id=int(chain_id), underlying=wrapped)
                if not cfg.get("stable_borrow_rate_enabled"):
                    return (
                        False,
                        "stable borrow is not enabled for wrapped native (use rate_mode=2)",
                    )

            tx = await encode_call(
                target=gateway,
                abi=WETH_GATEWAY_ABI,
                fn_name="borrowETH",
                args=[to_checksum_address(pool), int(amount), int(rate_mode), REFERRAL_CODE],
                from_address=strategy,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:  # noqa: BLE001
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
        strategy = self.wallet_address
        amount = int(amount)
        if amount <= 0 and not repay_full:
            return False, "amount must be positive"

        rate_mode = int(rate_mode)
        if rate_mode not in (STABLE_RATE_MODE, VARIABLE_RATE_MODE):
            return False, "rate_mode must be 1 (stable) or 2 (variable)"

        try:
            entry = self._entry(int(chain_id))
            pool = entry.get("pool")
            gateway = entry.get("wrapped_native_gateway")
            if not pool:
                raise ValueError(f"pool not configured for chain_id={chain_id}")
            if not gateway:
                raise ValueError(
                    f"wrapped_native_gateway not configured for chain_id={chain_id}"
                )

            repay_amount = MAX_UINT256 if repay_full else int(amount)

            if repay_full:
                wrapped = await self._wrapped_native(chain_id=int(chain_id))
                _, stable_debt, variable_debt = await self._reserve_tokens(
                    chain_id=int(chain_id), underlying=wrapped
                )
                debt_token = variable_debt if rate_mode == VARIABLE_RATE_MODE else stable_debt
                if str(debt_token).strip().lower() == ZERO_ADDRESS:
                    return False, "debt token address not found for wrapped native"

                debt = await get_token_balance(
                    debt_token,
                    int(chain_id),
                    strategy,
                    block_identifier="pending",
                )
                if debt <= 0:
                    return True, None

                native_balance = await get_token_balance(
                    None,
                    int(chain_id),
                    strategy,
                    block_identifier="pending",
                )
                buffer_wei = max(1, int(debt) // 10_000)  # 0.01%
                value = int(debt) + buffer_wei
                if native_balance < value:
                    if native_balance < int(debt):
                        return (
                            False,
                            f"insufficient native balance for repay_full (debt_wei={debt}, balance_wei={native_balance})",
                        )
                    value = int(debt)
            else:
                value = int(amount)

            tx = await encode_call(
                target=gateway,
                abi=WETH_GATEWAY_ABI,
                fn_name="repayETH",
                args=[to_checksum_address(pool), int(repay_amount), int(rate_mode), strategy],
                from_address=strategy,
                chain_id=int(chain_id),
                value=int(value),
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
