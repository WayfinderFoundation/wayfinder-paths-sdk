from __future__ import annotations

from typing import Any

from eth_utils import to_checksum_address

from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter
from wayfinder_paths.core.constants.aave_v3_abi import (
    POOL_ABI,
    REWARDS_CONTROLLER_ABI,
    UI_INCENTIVE_DATA_PROVIDER_V3_ABI,
    UI_POOL_DATA_PROVIDER_ABI,
    UI_POOL_RESERVE_KEYS,
    WETH_ABI,
    WRAPPED_TOKEN_GATEWAY_V3_ABI,
)
from wayfinder_paths.core.constants.aave_v3_contracts import AAVE_V3_BY_CHAIN
from wayfinder_paths.core.constants.base import MAX_UINT256, SECONDS_PER_YEAR
from wayfinder_paths.core.utils import web3 as web3_utils
from wayfinder_paths.core.utils.interest import RAY, apr_to_apy, ray_to_apr
from wayfinder_paths.core.utils.symbols import is_stable_symbol, normalize_symbol
from wayfinder_paths.core.utils.tokens import ensure_allowance, get_token_balance
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction

VARIABLE_RATE_MODE = 2
REFERRAL_CODE = 0


def _reserve_to_dict(reserve: Any, reserve_keys: list[str]) -> dict[str, Any]:
    if isinstance(reserve, dict):
        return dict(reserve)
    return dict(zip(reserve_keys, reserve, strict=False))


def _compute_supply_cap_headroom(
    reserve: dict[str, Any], decimals: int
) -> tuple[int | None, int | None]:
    supply_cap_tokens = int(reserve.get("supplyCap") or 0)
    if supply_cap_tokens <= 0:
        return (None, None)
    unit = 10 ** max(0, int(decimals))
    supply_cap_wei = supply_cap_tokens * unit

    available = int(reserve.get("availableLiquidity") or 0)
    scaled_variable_debt = int(reserve.get("totalScaledVariableDebt") or 0)
    variable_index = int(reserve.get("variableBorrowIndex") or 0)
    current_variable_debt = (scaled_variable_debt * variable_index) // RAY

    total_supplied = available + current_variable_debt
    headroom = supply_cap_wei - total_supplied
    if headroom < 0:
        headroom = 0
    return (headroom, supply_cap_tokens)


def _base_currency_to_ref(base_currency: Any) -> tuple[int, float]:
    # base_currency: (marketReferenceCurrencyUnit, marketReferenceCurrencyPriceInUsd, networkBaseTokenPriceInUsd, networkBaseTokenPriceDecimals)
    try:
        ref_unit = int(base_currency[0]) if base_currency else 1
    except (TypeError, ValueError):
        ref_unit = 1
    if not ref_unit:
        ref_unit = 1

    try:
        ref_usd_raw = int(base_currency[1]) if base_currency else 0
    except (TypeError, ValueError):
        ref_usd_raw = 0

    try:
        ref_usd_decimals = int(base_currency[3]) if base_currency else 0
    except (TypeError, ValueError):
        ref_usd_decimals = 0

    ref_usd = (
        ref_usd_raw / (10**ref_usd_decimals)
        if ref_usd_decimals and ref_usd_decimals > 0
        else float(ref_usd_raw)
    )
    return (ref_unit, float(ref_usd))


def _reward_rows(rewards_info: Any) -> list[Any]:
    try:
        return list(rewards_info[2] or [])
    except Exception:
        return []


def _compute_incentive_apr(
    rewards_info: Any,
    *,
    denom: float,
    side: str,
    include_details: bool,
) -> tuple[float, list[dict[str, Any]]]:
    apr_total = 0.0
    incentives: list[dict[str, Any]] = []

    for reward in _reward_rows(rewards_info):
        try:
            reward_addr = to_checksum_address(str(reward[1]))
            emission_per_second = int(reward[3] or 0)
            end_ts = int(reward[6] or 0)
            reward_price_feed = int(reward[7] or 0)
            reward_token_decimals = int(reward[8] or 0)
            price_feed_decimals = int(reward[10] or 0)
        except Exception:  # noqa: BLE001
            continue

        price_usd_r = (
            float(reward_price_feed) / (10**price_feed_decimals)
            if price_feed_decimals and reward_price_feed
            else 0.0
        )

        annual_rewards_usd = (
            (float(emission_per_second) / (10**reward_token_decimals))
            * SECONDS_PER_YEAR
            * price_usd_r
            if emission_per_second and reward_token_decimals >= 0 and price_usd_r
            else 0.0
        )
        apr = (
            float(annual_rewards_usd) / float(denom)
            if denom and annual_rewards_usd
            else 0.0
        )
        if apr:
            apr_total += apr

        if include_details:
            incentives.append(
                {
                    "side": side,
                    "token": reward_addr,
                    "symbol": str(reward[0] or ""),
                    "apr": float(apr),
                    "emission_per_second": int(emission_per_second),
                    "distribution_end": int(end_ts) or None,
                    "price_usd": float(price_usd_r),
                }
            )

    return float(apr_total), incentives


class AaveV3Adapter(BaseAdapter):
    adapter_type = "AAVE_V3"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        signing_callback=None,
    ) -> None:
        super().__init__("aave_v3_adapter", config or {})
        cfg = config or {}
        self.signing_callback = signing_callback

        strategy_addr = (cfg.get("strategy_wallet") or {}).get("address")
        self.wallet_address: str | None = (
            to_checksum_address(strategy_addr) if strategy_addr else None
        )

        # Cache: (chain_id, underlying.lower()) -> variableDebtTokenAddress
        self._variable_debt_token_by_chain_underlying: dict[tuple[int, str], str] = {}
        # Cache: chain_id -> wrapped native token address (WETH/WBNB/WMATIC/etc)
        self._wrapped_native_by_chain: dict[int, str] = {}

    def _entry(self, chain_id: int) -> dict[str, str]:
        entry = AAVE_V3_BY_CHAIN.get(int(chain_id))
        if not entry:
            raise ValueError(f"Unsupported Aave v3 chain_id={chain_id}")
        return entry

    async def _pool(self, *, chain_id: int) -> str:
        return to_checksum_address(self._entry(int(chain_id))["pool"])

    async def _provider(self, *, chain_id: int) -> str:
        return to_checksum_address(
            self._entry(int(chain_id))["pool_addresses_provider"]
        )

    async def _ui_pool(self, *, chain_id: int) -> str:
        return to_checksum_address(self._entry(int(chain_id))["ui_pool_data_provider"])

    async def _ui_incentives(self, *, chain_id: int) -> str:
        return to_checksum_address(
            self._entry(int(chain_id))["ui_incentive_data_provider"]
        )

    async def _rewards_controller(self, *, chain_id: int) -> str:
        return to_checksum_address(self._entry(int(chain_id))["rewards_controller"])

    async def _wrapped_native(self, *, chain_id: int) -> str:
        cached = self._wrapped_native_by_chain.get(int(chain_id))
        if cached:
            return cached

        gateway = self._entry(int(chain_id)).get("wrapped_token_gateway")
        if not gateway:
            raise ValueError(
                f"wrapped_token_gateway not configured for chain_id={chain_id}"
            )

        async with web3_utils.web3_from_chain_id(int(chain_id)) as web3:
            gw = web3.eth.contract(
                address=to_checksum_address(gateway),
                abi=WRAPPED_TOKEN_GATEWAY_V3_ABI,
            )
            wrapped = await gw.functions.getWETHAddress().call(
                block_identifier="pending"
            )
            wrapped = to_checksum_address(str(wrapped))
            self._wrapped_native_by_chain[int(chain_id)] = wrapped
            return wrapped

    async def get_all_markets(
        self,
        *,
        chain_id: int,
        include_rewards: bool = True,
    ) -> tuple[bool, list[dict[str, Any]] | str]:
        try:
            entry = self._entry(int(chain_id))
            ui_pool_addr = to_checksum_address(entry["ui_pool_data_provider"])
            provider_addr = to_checksum_address(entry["pool_addresses_provider"])

            reserves_incentives: dict[str, Any] = {}
            async with web3_utils.web3_from_chain_id(int(chain_id)) as web3:
                if include_rewards:
                    try:
                        ui_incentives_addr = to_checksum_address(
                            entry["ui_incentive_data_provider"]
                        )
                        ui_incentives = web3.eth.contract(
                            address=ui_incentives_addr,
                            abi=UI_INCENTIVE_DATA_PROVIDER_V3_ABI,
                        )
                        inc_rows = (
                            await ui_incentives.functions.getReservesIncentivesData(
                                provider_addr
                            ).call(block_identifier="pending")
                        )
                        for row in inc_rows or []:
                            try:
                                underlying = to_checksum_address(str(row[0]))
                            except Exception:  # noqa: BLE001
                                continue
                            reserves_incentives[underlying.lower()] = row
                    except Exception:  # noqa: BLE001
                        reserves_incentives = {}

                ui_pool = web3.eth.contract(
                    address=ui_pool_addr, abi=UI_POOL_DATA_PROVIDER_ABI
                )
                reserves, base_currency = await ui_pool.functions.getReservesData(
                    provider_addr
                ).call(block_identifier="pending")

                ref_unit, ref_usd = _base_currency_to_ref(base_currency)

                reserve_keys = UI_POOL_RESERVE_KEYS
                markets: list[dict[str, Any]] = []

                for reserve in reserves or []:
                    r = _reserve_to_dict(reserve, reserve_keys)

                    underlying = to_checksum_address(str(r.get("underlyingAsset")))
                    symbol_raw = str(r.get("symbol") or "")
                    decimals = int(r.get("decimals") or 18)
                    a_token = to_checksum_address(str(r.get("aTokenAddress")))
                    v_debt = to_checksum_address(str(r.get("variableDebtTokenAddress")))
                    self._variable_debt_token_by_chain_underlying[
                        (int(chain_id), underlying.lower())
                    ] = v_debt

                    liquidity_rate_ray = int(r.get("liquidityRate") or 0)
                    variable_borrow_rate_ray = int(r.get("variableBorrowRate") or 0)

                    price_market_ref = int(r.get("priceInMarketReferenceCurrency") or 0)
                    try:
                        price_market_ref_float = (
                            float(price_market_ref) / ref_unit if ref_unit else 0.0
                        )
                    except ZeroDivisionError:
                        price_market_ref_float = 0.0
                    price_usd = float(price_market_ref_float) * float(ref_usd)

                    supply_apr = ray_to_apr(liquidity_rate_ray)
                    borrow_apr = ray_to_apr(variable_borrow_rate_ray)

                    available_liquidity = int(r.get("availableLiquidity") or 0)
                    scaled_variable_debt = int(r.get("totalScaledVariableDebt") or 0)
                    variable_index = int(r.get("variableBorrowIndex") or 0)
                    total_variable_debt = (scaled_variable_debt * variable_index) // RAY
                    tvl = available_liquidity + total_variable_debt

                    symbol_canonical = normalize_symbol(symbol_raw) or normalize_symbol(
                        underlying
                    )

                    headroom_wei, supply_cap_tokens = _compute_supply_cap_headroom(
                        r, decimals
                    )

                    base_supply_apy = float(apr_to_apy(supply_apr))
                    base_borrow_apy = float(apr_to_apy(borrow_apr))

                    market_row: dict[str, Any] = {
                        "chain_id": int(chain_id),
                        "pool": to_checksum_address(entry["pool"]),
                        "underlying": underlying,
                        "symbol": symbol_raw,
                        "symbol_canonical": symbol_canonical,
                        "decimals": int(decimals),
                        "a_token": a_token,
                        "variable_debt_token": v_debt,
                        "is_active": bool(r.get("isActive")),
                        "is_frozen": bool(r.get("isFrozen")),
                        "is_paused": bool(r.get("isPaused")),
                        "is_siloed_borrowing": bool(r.get("isSiloedBorrowing")),
                        "is_stablecoin": bool(is_stable_symbol(symbol_raw)),
                        "usage_as_collateral_enabled": bool(
                            r.get("usageAsCollateralEnabled")
                        ),
                        "borrowing_enabled": bool(r.get("borrowingEnabled")),
                        "ltv_bps": int(r.get("baseLTVasCollateral") or 0),
                        "liquidation_threshold_bps": int(
                            r.get("reserveLiquidationThreshold") or 0
                        ),
                        "price_usd": float(price_usd),
                        "supply_apr": float(supply_apr),
                        "supply_apy": float(base_supply_apy),
                        "variable_borrow_apr": float(borrow_apr),
                        "variable_borrow_apy": float(base_borrow_apy),
                        "available_liquidity": int(available_liquidity),
                        "total_variable_debt": int(total_variable_debt),
                        "tvl": int(tvl),
                        "borrow_cap": int(r.get("borrowCap") or 0) or None,
                        "supply_cap": supply_cap_tokens,
                        "supply_cap_headroom": headroom_wei,
                    }

                    if include_rewards:
                        reward_supply_apr = 0.0
                        reward_borrow_apr = 0.0
                        incentives_out: list[dict[str, Any]] = []

                        denom_supply = (
                            (available_liquidity + total_variable_debt)
                            / (10**decimals)
                            * price_usd
                            if price_usd
                            and (available_liquidity + total_variable_debt) > 0
                            else 0.0
                        )
                        denom_borrow = (
                            total_variable_debt / (10**decimals) * price_usd
                            if price_usd and total_variable_debt > 0
                            else 0.0
                        )

                        inc_row = reserves_incentives.get(underlying.lower())
                        if inc_row:
                            a_inc = inc_row[1] if len(inc_row) > 1 else None
                            v_inc = inc_row[2] if len(inc_row) > 2 else None

                            supply_apr_inc, supply_incs = _compute_incentive_apr(
                                a_inc,
                                denom=float(denom_supply),
                                side="supply",
                                include_details=True,
                            )
                            borrow_apr_inc, borrow_incs = _compute_incentive_apr(
                                v_inc,
                                denom=float(denom_borrow),
                                side="borrow",
                                include_details=True,
                            )
                            reward_supply_apr += supply_apr_inc
                            reward_borrow_apr += borrow_apr_inc
                            incentives_out.extend(supply_incs)
                            incentives_out.extend(borrow_incs)

                        market_row["reward_supply_apr"] = float(reward_supply_apr)
                        market_row["reward_borrow_apr"] = float(reward_borrow_apr)
                        market_row["supply_apy_with_rewards"] = float(
                            base_supply_apy + apr_to_apy(reward_supply_apr)
                        )
                        market_row["borrow_apy_with_rewards"] = float(
                            base_borrow_apy - apr_to_apy(reward_borrow_apr)
                        )
                        market_row["incentives"] = incentives_out

                    markets.append(market_row)

                return True, markets
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def get_full_user_state(
        self,
        *,
        account: str,
        include_zero_positions: bool = False,
        include_rewards: bool = True,
    ) -> tuple[bool, dict[str, Any] | str]:
        """Query all supported Aave V3 chains and merge results."""
        account = to_checksum_address(account)
        all_positions: list[dict[str, Any]] = []
        chains_queried: list[dict[str, Any]] = []
        errors: list[str] = []

        for cid in AAVE_V3_BY_CHAIN:
            ok, result = await self.get_full_user_state_per_chain(
                chain_id=cid,
                account=account,
                include_zero_positions=include_zero_positions,
                include_rewards=include_rewards,
            )
            if ok:
                chain_data = result  # type: ignore[assignment]
                all_positions.extend(chain_data.get("positions", []))
                chains_queried.append(
                    {
                        "chain_id": cid,
                        "pool": chain_data.get("pool"),
                        "userEmodeCategoryId": chain_data.get("userEmodeCategoryId", 0),
                    }
                )
            else:
                errors.append(f"chain {cid}: {result}")

        if not chains_queried and errors:
            return False, "; ".join(errors)

        return True, {
            "protocol": "aave_v3",
            "account": account,
            "chains": chains_queried,
            "positions": all_positions,
            "errors": errors,
        }

    async def get_full_user_state_per_chain(
        self,
        *,
        chain_id: int,
        account: str,
        include_zero_positions: bool = False,
        include_rewards: bool = True,
    ) -> tuple[bool, dict[str, Any] | str]:
        try:
            entry = self._entry(int(chain_id))
            ui_pool_addr = to_checksum_address(entry["ui_pool_data_provider"])
            provider_addr = to_checksum_address(entry["pool_addresses_provider"])

            account = to_checksum_address(account)

            async with web3_utils.web3_from_chain_id(int(chain_id)) as web3:
                ui_pool = web3.eth.contract(
                    address=ui_pool_addr, abi=UI_POOL_DATA_PROVIDER_ABI
                )
                reserves, base_currency = await ui_pool.functions.getReservesData(
                    provider_addr
                ).call(block_identifier="pending")
                user_reserves, user_emode = await ui_pool.functions.getUserReservesData(
                    provider_addr, account
                ).call(block_identifier="pending")

                ref_unit, ref_usd = _base_currency_to_ref(base_currency)

                reserve_keys = UI_POOL_RESERVE_KEYS
                by_underlying: dict[str, dict[str, Any]] = {}
                for reserve in reserves or []:
                    r = _reserve_to_dict(reserve, reserve_keys)
                    underlying = to_checksum_address(str(r.get("underlyingAsset")))
                    by_underlying[underlying.lower()] = r

                reserves_incentives: dict[str, Any] = {}
                user_rewards_by_underlying: dict[str, list[dict[str, Any]]] = {}
                if include_rewards:
                    try:
                        ui_incentives_addr = to_checksum_address(
                            entry["ui_incentive_data_provider"]
                        )
                        ui_incentives = web3.eth.contract(
                            address=ui_incentives_addr,
                            abi=UI_INCENTIVE_DATA_PROVIDER_V3_ABI,
                        )

                        try:
                            inc_rows = (
                                await ui_incentives.functions.getReservesIncentivesData(
                                    provider_addr
                                ).call(block_identifier="pending")
                            )
                            for row in inc_rows or []:
                                try:
                                    underlying = to_checksum_address(str(row[0]))
                                except Exception:  # noqa: BLE001
                                    continue
                                reserves_incentives[underlying.lower()] = row
                        except Exception:  # noqa: BLE001
                            reserves_incentives = {}

                        user_inc = (
                            await ui_incentives.functions.getUserReservesIncentivesData(
                                provider_addr, account
                            ).call(block_identifier="pending")
                        )

                        for row in user_inc or []:
                            try:
                                underlying = to_checksum_address(str(row[0]))
                            except Exception:  # noqa: BLE001
                                continue
                            out_rewards: list[dict[str, Any]] = []
                            for side_idx, side in (
                                (1, "supply"),
                                (2, "borrow"),
                                (3, "stable_borrow"),
                            ):
                                try:
                                    inc = row[side_idx]
                                    rewards = list(inc[2] or [])
                                except Exception:
                                    rewards = []
                                for rwd in rewards:
                                    try:
                                        out_rewards.append(
                                            {
                                                "side": side,
                                                "token": to_checksum_address(
                                                    str(rwd[2])
                                                ),
                                                "symbol": str(rwd[0] or ""),
                                                "unclaimed": int(rwd[3] or 0),
                                                "price_usd": (
                                                    float(int(rwd[5] or 0))
                                                    / (10 ** int(rwd[6] or 0))
                                                    if int(rwd[5] or 0)
                                                    and int(rwd[6] or 0)
                                                    else 0.0
                                                ),
                                                "price_feed_decimals": int(rwd[6] or 0),
                                                "reward_token_decimals": int(
                                                    rwd[7] or 0
                                                ),
                                            }
                                        )
                                    except Exception:  # noqa: BLE001
                                        continue
                            user_rewards_by_underlying[underlying.lower()] = out_rewards
                    except Exception:
                        reserves_incentives = {}
                        user_rewards_by_underlying = {}

            positions: list[dict[str, Any]] = []
            for row in user_reserves or []:
                try:
                    underlying = to_checksum_address(str(row[0]))
                except Exception:  # noqa: BLE001
                    continue

                reserve = by_underlying.get(underlying.lower())
                if not reserve:
                    continue

                decimals = int(reserve.get("decimals") or 18)
                symbol_raw = str(reserve.get("symbol") or "")

                liquidity_index = int(reserve.get("liquidityIndex") or 0)
                variable_borrow_index = int(reserve.get("variableBorrowIndex") or 0)

                scaled_supply = int(row[1] or 0)
                scaled_var_debt = int(row[3] or 0)
                stable_debt = 0
                is_collateral = bool(row[2])

                supply_raw = (
                    (scaled_supply * liquidity_index) // RAY if scaled_supply else 0
                )
                variable_debt_raw = (
                    (scaled_var_debt * variable_borrow_index) // RAY
                    if scaled_var_debt
                    else 0
                )

                price_market_ref = int(
                    reserve.get("priceInMarketReferenceCurrency") or 0
                )
                price_usd = (
                    (float(price_market_ref) / ref_unit) * float(ref_usd)
                    if ref_unit and price_market_ref
                    else 0.0
                )

                supply_usd = (
                    float(supply_raw) / (10**decimals) * price_usd
                    if supply_raw and price_usd
                    else 0.0
                )
                borrow_usd = (
                    float(variable_debt_raw) / (10**decimals) * price_usd
                    if variable_debt_raw and price_usd
                    else 0.0
                )

                if (
                    not include_zero_positions
                    and supply_raw <= 0
                    and variable_debt_raw <= 0
                    and stable_debt <= 0
                ):
                    continue

                liquidity_rate_ray = int(reserve.get("liquidityRate") or 0)
                variable_borrow_rate_ray = int(reserve.get("variableBorrowRate") or 0)
                supply_apy = float(apr_to_apy(ray_to_apr(liquidity_rate_ray)))
                variable_borrow_apy = float(
                    apr_to_apy(ray_to_apr(variable_borrow_rate_ray))
                )

                reward_supply_apr = 0.0
                reward_borrow_apr = 0.0
                if include_rewards:
                    inc_row = reserves_incentives.get(underlying.lower())
                    if inc_row:
                        a_inc = inc_row[1] if len(inc_row) > 1 else None
                        v_inc = inc_row[2] if len(inc_row) > 2 else None

                        available_liquidity = int(
                            reserve.get("availableLiquidity") or 0
                        )
                        scaled_variable_debt = int(
                            reserve.get("totalScaledVariableDebt") or 0
                        )
                        variable_index = int(reserve.get("variableBorrowIndex") or 0)
                        total_variable_debt = (
                            scaled_variable_debt * variable_index
                        ) // RAY
                        denom_supply = (
                            (available_liquidity + total_variable_debt)
                            / (10**decimals)
                            * price_usd
                            if price_usd
                            and (available_liquidity + total_variable_debt) > 0
                            else 0.0
                        )
                        denom_borrow = (
                            total_variable_debt / (10**decimals) * price_usd
                            if price_usd and total_variable_debt > 0
                            else 0.0
                        )

                        reward_supply_apr, _ = _compute_incentive_apr(
                            a_inc,
                            denom=float(denom_supply),
                            side="supply",
                            include_details=False,
                        )
                        reward_borrow_apr, _ = _compute_incentive_apr(
                            v_inc,
                            denom=float(denom_borrow),
                            side="borrow",
                            include_details=False,
                        )

                positions.append(
                    {
                        "chain_id": int(chain_id),
                        "underlying": underlying,
                        "symbol": symbol_raw,
                        "symbol_canonical": normalize_symbol(symbol_raw)
                        or normalize_symbol(underlying),
                        "decimals": decimals,
                        "a_token": to_checksum_address(
                            str(reserve.get("aTokenAddress"))
                        ),
                        "variable_debt_token": to_checksum_address(
                            str(reserve.get("variableDebtTokenAddress"))
                        ),
                        "usage_as_collateral": is_collateral,
                        "supply_raw": int(supply_raw),
                        "variable_borrow_raw": int(variable_debt_raw),
                        "stable_borrow_raw": int(stable_debt),
                        "supply_usd": float(supply_usd),
                        "variable_borrow_usd": float(borrow_usd),
                        "price_usd": float(price_usd),
                        "supply_apy": supply_apy,
                        "variable_borrow_apy": variable_borrow_apy,
                        "reward_supply_apr": reward_supply_apr,
                        "reward_borrow_apr": reward_borrow_apr,
                        "supply_apy_with_rewards": supply_apy
                        + apr_to_apy(reward_supply_apr),
                        "borrow_apy_with_rewards": variable_borrow_apy
                        - apr_to_apy(reward_borrow_apr),
                        "rewards": user_rewards_by_underlying.get(underlying.lower())
                        or [],
                    }
                )

            return True, {
                "protocol": "aave_v3",
                "chain_id": int(chain_id),
                "pool": entry["pool"],
                "account": account,
                "userEmodeCategoryId": int(user_emode or 0),
                "positions": positions,
            }
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def lend(
        self,
        *,
        underlying_token: str,
        qty: int,
        chain_id: int,
        native: bool = False,
    ) -> tuple[bool, Any]:
        strategy = self.wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"
        qty = int(qty)
        if qty <= 0:
            return False, "qty must be positive"

        try:
            pool = await self._pool(chain_id=int(chain_id))

            if native:
                wrapped = await self._wrapped_native(chain_id=int(chain_id))
                wrap_tx = await encode_call(
                    target=wrapped,
                    abi=WETH_ABI,
                    fn_name="deposit",
                    args=[],
                    from_address=strategy,
                    chain_id=int(chain_id),
                    value=qty,
                )
                wrap_hash = await send_transaction(
                    wrap_tx, self.signing_callback
                )

                approved = await ensure_allowance(
                    token_address=wrapped,
                    owner=strategy,
                    spender=pool,
                    amount=qty,
                    chain_id=int(chain_id),
                    signing_callback=self.signing_callback,
                    approval_amount=MAX_UINT256,
                )
                if not approved[0]:
                    return approved

                supply_tx = await encode_call(
                    target=pool,
                    abi=POOL_ABI,
                    fn_name="supply",
                    args=[wrapped, qty, strategy, REFERRAL_CODE],
                    from_address=strategy,
                    chain_id=int(chain_id),
                )
                supply_hash = await send_transaction(
                    supply_tx, self.signing_callback
                )
                return True, {"wrap_tx": wrap_hash, "supply_tx": supply_hash}

            asset = to_checksum_address(underlying_token)
            approved = await ensure_allowance(
                token_address=asset,
                owner=strategy,
                spender=pool,
                amount=qty,
                chain_id=int(chain_id),
                signing_callback=self.signing_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved[0]:
                return approved

            tx = await encode_call(
                target=pool,
                abi=POOL_ABI,
                fn_name="supply",
                args=[asset, qty, strategy, REFERRAL_CODE],
                from_address=strategy,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.signing_callback)
            return True, txn_hash
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def unlend(
        self,
        *,
        underlying_token: str,
        qty: int,
        chain_id: int,
        native: bool = False,
        withdraw_full: bool = False,
    ) -> tuple[bool, Any]:
        strategy = self.wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"
        qty = int(qty)
        if qty <= 0 and not withdraw_full:
            return False, "qty must be positive"

        try:
            pool = await self._pool(chain_id=int(chain_id))
            amount = MAX_UINT256 if withdraw_full else qty

            if native:
                wrapped = await self._wrapped_native(chain_id=int(chain_id))
                before = await get_token_balance(
                    wrapped, int(chain_id), strategy, block_identifier="pending"
                )

                withdraw_tx = await encode_call(
                    target=pool,
                    abi=POOL_ABI,
                    fn_name="withdraw",
                    args=[wrapped, amount, strategy],
                    from_address=strategy,
                    chain_id=int(chain_id),
                )
                withdraw_hash = await send_transaction(
                    withdraw_tx, self.signing_callback
                )

                after = await get_token_balance(
                    wrapped, int(chain_id), strategy, block_identifier="pending"
                )
                unwrap_amount = max(0, int(after) - int(before))
                if unwrap_amount <= 0:
                    return True, {"withdraw_tx": withdraw_hash, "unwrap_tx": None}

                unwrap_tx = await encode_call(
                    target=wrapped,
                    abi=WETH_ABI,
                    fn_name="withdraw",
                    args=[int(unwrap_amount)],
                    from_address=strategy,
                    chain_id=int(chain_id),
                )
                unwrap_hash = await send_transaction(
                    unwrap_tx, self.signing_callback
                )
                return True, {"withdraw_tx": withdraw_hash, "unwrap_tx": unwrap_hash}

            asset = to_checksum_address(underlying_token)
            tx = await encode_call(
                target=pool,
                abi=POOL_ABI,
                fn_name="withdraw",
                args=[asset, amount, strategy],
                from_address=strategy,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.signing_callback)
            return True, txn_hash
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def borrow(
        self,
        *,
        underlying_token: str,
        qty: int,
        chain_id: int,
        native: bool = False,
    ) -> tuple[bool, Any]:
        strategy = self.wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"
        qty = int(qty)
        if qty <= 0:
            return False, "qty must be positive"

        try:
            pool = await self._pool(chain_id=int(chain_id))
            if native:
                wrapped = await self._wrapped_native(chain_id=int(chain_id))
                borrow_tx = await encode_call(
                    target=pool,
                    abi=POOL_ABI,
                    fn_name="borrow",
                    args=[wrapped, qty, VARIABLE_RATE_MODE, REFERRAL_CODE, strategy],
                    from_address=strategy,
                    chain_id=int(chain_id),
                )
                borrow_hash = await send_transaction(
                    borrow_tx, self.signing_callback
                )

                unwrap_tx = await encode_call(
                    target=wrapped,
                    abi=WETH_ABI,
                    fn_name="withdraw",
                    args=[qty],
                    from_address=strategy,
                    chain_id=int(chain_id),
                )
                unwrap_hash = await send_transaction(
                    unwrap_tx, self.signing_callback
                )
                return True, {"borrow_tx": borrow_hash, "unwrap_tx": unwrap_hash}

            asset = to_checksum_address(underlying_token)
            tx = await encode_call(
                target=pool,
                abi=POOL_ABI,
                fn_name="borrow",
                args=[asset, qty, VARIABLE_RATE_MODE, REFERRAL_CODE, strategy],
                from_address=strategy,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.signing_callback)
            return True, txn_hash
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def repay(
        self,
        *,
        underlying_token: str,
        qty: int,
        chain_id: int,
        native: bool = False,
        repay_full: bool = False,
    ) -> tuple[bool, Any]:
        strategy = self.wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"
        qty = int(qty)
        if qty <= 0 and not repay_full:
            return False, "qty must be positive"

        try:
            pool = await self._pool(chain_id=int(chain_id))

            if native:
                wrapped = await self._wrapped_native(chain_id=int(chain_id))
                repay_amount = MAX_UINT256 if repay_full else qty

                if repay_full:
                    # Read current variable debt (debt token balance) and wrap a small buffer.
                    v_debt = self._variable_debt_token_by_chain_underlying.get(
                        (int(chain_id), wrapped.lower())
                    )
                    if not v_debt:
                        ok, markets = await self.get_all_markets(
                            chain_id=int(chain_id), include_rewards=False
                        )
                        if not ok or not isinstance(markets, list):
                            return (
                                False,
                                f"failed to resolve reserves for chain_id={chain_id}",
                            )
                        for m in markets:
                            if (
                                str(m.get("underlying") or "").lower()
                                == wrapped.lower()
                            ):
                                v_debt = str(m.get("variable_debt_token") or "")
                                break
                        if not v_debt:
                            return (
                                False,
                                "could not resolve variable debt token for wrapped native",
                            )
                        self._variable_debt_token_by_chain_underlying[
                            (int(chain_id), wrapped.lower())
                        ] = to_checksum_address(v_debt)

                    debt = await get_token_balance(
                        str(v_debt),
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
                    value = qty

                wrap_tx = await encode_call(
                    target=wrapped,
                    abi=WETH_ABI,
                    fn_name="deposit",
                    args=[],
                    from_address=strategy,
                    chain_id=int(chain_id),
                    value=int(value),
                )
                wrap_hash = await send_transaction(
                    wrap_tx, self.signing_callback
                )

                approved = await ensure_allowance(
                    token_address=wrapped,
                    owner=strategy,
                    spender=pool,
                    amount=MAX_UINT256 if repay_full else int(value),
                    chain_id=int(chain_id),
                    signing_callback=self.signing_callback,
                    approval_amount=MAX_UINT256,
                )
                if not approved[0]:
                    return approved

                repay_tx = await encode_call(
                    target=pool,
                    abi=POOL_ABI,
                    fn_name="repay",
                    args=[wrapped, int(repay_amount), VARIABLE_RATE_MODE, strategy],
                    from_address=strategy,
                    chain_id=int(chain_id),
                )
                repay_hash = await send_transaction(
                    repay_tx, self.signing_callback
                )
                return True, {"wrap_tx": wrap_hash, "repay_tx": repay_hash}

            repay_amount = MAX_UINT256 if repay_full else qty
            asset = to_checksum_address(underlying_token)
            allowance_target = MAX_UINT256 if repay_full else qty
            approved = await ensure_allowance(
                token_address=asset,
                owner=strategy,
                spender=pool,
                amount=allowance_target,
                chain_id=int(chain_id),
                signing_callback=self.signing_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved[0]:
                return approved

            tx = await encode_call(
                target=pool,
                abi=POOL_ABI,
                fn_name="repay",
                args=[asset, int(repay_amount), VARIABLE_RATE_MODE, strategy],
                from_address=strategy,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.signing_callback)
            return True, txn_hash
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def set_collateral(
        self,
        *,
        underlying_token: str,
        chain_id: int,
        use_as_collateral: bool = True,
    ) -> tuple[bool, Any]:
        strategy = self.wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"

        try:
            pool = await self._pool(chain_id=int(chain_id))
            asset = to_checksum_address(underlying_token)
            tx = await encode_call(
                target=pool,
                abi=POOL_ABI,
                fn_name="setUserUseReserveAsCollateral",
                args=[asset, bool(use_as_collateral)],
                from_address=strategy,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.signing_callback)
            return True, txn_hash
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def remove_collateral(
        self,
        *,
        underlying_token: str,
        chain_id: int,
    ) -> tuple[bool, Any]:
        return await self.set_collateral(
            underlying_token=str(underlying_token),
            chain_id=int(chain_id),
            use_as_collateral=False,
        )

    async def claim_all_rewards(
        self,
        *,
        chain_id: int,
        assets: list[str] | None = None,
        to_address: str | None = None,
    ) -> tuple[bool, Any]:
        strategy = self.wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"

        try:
            entry = self._entry(int(chain_id))
            rewards_controller = to_checksum_address(entry["rewards_controller"])
            provider_addr = to_checksum_address(entry["pool_addresses_provider"])
            to_addr = to_checksum_address(to_address) if to_address else strategy

            if not assets:
                # Derive incentivized token addresses from the incentives data provider.
                ui_incentives_addr = to_checksum_address(
                    entry["ui_incentive_data_provider"]
                )
                async with web3_utils.web3_from_chain_id(int(chain_id)) as web3:
                    ui_incentives = web3.eth.contract(
                        address=ui_incentives_addr,
                        abi=UI_INCENTIVE_DATA_PROVIDER_V3_ABI,
                    )
                    inc_rows = await ui_incentives.functions.getReservesIncentivesData(
                        provider_addr
                    ).call(block_identifier="pending")

                assets_set: set[str] = set()
                for row in inc_rows or []:
                    for i in (1, 2, 3):
                        try:
                            token_addr = str((row[i] or [None])[0] or "")
                        except Exception:
                            token_addr = ""
                        if (
                            token_addr
                            and token_addr
                            != "0x0000000000000000000000000000000000000000"
                        ):
                            assets_set.add(to_checksum_address(token_addr))
                assets = sorted(assets_set)

            if not assets:
                return True, {"claimed": [], "note": "no incentivized assets found"}

            tx = await encode_call(
                target=rewards_controller,
                abi=REWARDS_CONTROLLER_ABI,
                fn_name="claimAllRewards",
                args=[[to_checksum_address(a) for a in assets], to_addr],
                from_address=strategy,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.signing_callback)
            return True, txn_hash
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
