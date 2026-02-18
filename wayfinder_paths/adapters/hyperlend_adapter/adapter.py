from __future__ import annotations

from typing import Any, Literal

from eth_utils import to_checksum_address

from wayfinder_paths.adapters.ledger_adapter.adapter import LedgerAdapter
from wayfinder_paths.adapters.token_adapter.adapter import TokenAdapter
from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter
from wayfinder_paths.core.adapters.models import LEND, UNLEND
from wayfinder_paths.core.clients.HyperlendClient import (
    HYPERLEND_CLIENT,
    AssetsView,
    LendRateHistory,
    MarketEntry,
    StableMarketsHeadroomResponse,
)
from wayfinder_paths.core.constants.base import MAX_UINT256
from wayfinder_paths.core.constants.chains import CHAIN_ID_HYPEREVM
from wayfinder_paths.core.constants.contracts import (
    HYPEREVM_WHYPE,
    HYPERLEND_POOL,
    HYPERLEND_POOL_ADDRESSES_PROVIDER,
    HYPERLEND_UI_POOL_DATA_PROVIDER,
    HYPERLEND_WRAPPED_TOKEN_GATEWAY,
)
from wayfinder_paths.core.constants.hyperlend_abi import (
    POOL_ABI,
    UI_POOL_DATA_PROVIDER_ABI,
    UI_POOL_RESERVE_KEYS,
    WETH_ABI,
    WRAPPED_TOKEN_GATEWAY_ABI,
)
from wayfinder_paths.core.utils.interest import RAY, apr_to_apy, ray_to_apr
from wayfinder_paths.core.utils.symbols import is_stable_symbol, normalize_symbol
from wayfinder_paths.core.utils.tokens import ensure_allowance, get_token_balance
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

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

    # Note: stable debt is not included here because it is not exposed
    # via UI_POOL_RESERVE_KEYS / UI_POOL_DATA_PROVIDER_ABI for tuple-based data.
    total_supplied = available + current_variable_debt
    headroom = supply_cap_wei - total_supplied
    if headroom < 0:
        headroom = 0
    return (headroom, supply_cap_tokens)


class HyperlendAdapter(BaseAdapter):
    adapter_type = "HYPERLEND"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        signing_callback=None,
        strategy_wallet_signing_callback=None,
    ) -> None:
        if signing_callback is None:
            signing_callback = strategy_wallet_signing_callback
        super().__init__("hyperlend_adapter", config)
        config = config or {}

        self.signing_callback = signing_callback

        self.ledger_adapter = LedgerAdapter()
        self.token_adapter = TokenAdapter()
        strategy_wallet = config.get("strategy_wallet") or {}
        strategy_addr = strategy_wallet.get("address")

        self.wallet_address: str | None = (
            to_checksum_address(strategy_addr) if strategy_addr else None
        )
        self._variable_debt_token_by_underlying: dict[str, str] = {}


    async def get_stable_markets(
        self,
        *,
        required_underlying_tokens: float | None = None,
        buffer_bps: int | None = None,
        min_buffer_tokens: float | None = None,
    ) -> tuple[bool, StableMarketsHeadroomResponse | str]:
        try:
            data = await HYPERLEND_CLIENT.get_stable_markets(
                required_underlying_tokens=required_underlying_tokens,
                buffer_bps=buffer_bps,
                min_buffer_tokens=min_buffer_tokens,
            )
            return True, data
        except Exception as exc:
            return False, str(exc)

    async def get_assets_view(
        self,
        *,
        user_address: str,
    ) -> tuple[bool, AssetsView | str]:
        try:
            data = await HYPERLEND_CLIENT.get_assets_view(user_address=user_address)
            return True, data
        except Exception as exc:
            return False, str(exc)

    async def get_full_user_state(
        self,
        *,
        account: str,
        include_zero_positions: bool = False,
    ) -> tuple[bool, dict[str, Any] | str]:
        ok, view = await self.get_assets_view(user_address=account)
        if not ok:
            return False, str(view)

        assets = view.get("assets", []) if isinstance(view, dict) else []
        if include_zero_positions:
            positions = assets
        else:
            positions = [
                a
                for a in assets
                if float(a.get("supply", 0) or 0) > 0
                or float(a.get("variable_borrow", 0) or 0) > 0
            ]

        return (
            True,
            {
                "protocol": "hyperlend",
                "account": account,
                "positions": positions,
                "accountData": view.get("account_data")
                if isinstance(view, dict)
                else {},
                "assetsView": view,
            },
        )

    async def get_market_entry(
        self,
        *,
        token: str,
    ) -> tuple[bool, MarketEntry | str]:
        try:
            data = await HYPERLEND_CLIENT.get_market_entry(token=token)
            return True, data
        except Exception as exc:
            return False, str(exc)

    async def get_lend_rate_history(
        self,
        *,
        token: str,
        lookback_hours: int,
        force_refresh: bool | None = None,
    ) -> tuple[bool, LendRateHistory | str]:
        try:
            data = await HYPERLEND_CLIENT.get_lend_rate_history(
                token=token,
                lookback_hours=lookback_hours,
                force_refresh=force_refresh,
            )
            return True, data
        except Exception as exc:
            return False, str(exc)

    async def get_all_markets(self) -> tuple[bool, list[dict[str, Any]] | str]:
        try:
            async with web3_from_chain_id(CHAIN_ID_HYPEREVM) as web3:
                ui_pool = web3.eth.contract(
                    address=HYPERLEND_UI_POOL_DATA_PROVIDER,
                    abi=UI_POOL_DATA_PROVIDER_ABI,
                )

                reserves, base_currency = await ui_pool.functions.getReservesData(
                    HYPERLEND_POOL_ADDRESSES_PROVIDER
                ).call(block_identifier="pending")

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

                reserve_keys = UI_POOL_RESERVE_KEYS

                markets: list[dict[str, Any]] = []
                for reserve in reserves or []:
                    r = _reserve_to_dict(reserve, reserve_keys)

                    underlying = to_checksum_address(str(r.get("underlyingAsset")))
                    symbol_raw = r.get("symbol") or ""
                    decimals = int(r.get("decimals") or 18)
                    a_token = to_checksum_address(str(r.get("aTokenAddress")))
                    v_debt = to_checksum_address(str(r.get("variableDebtTokenAddress")))
                    self._variable_debt_token_by_underlying[underlying.lower()] = v_debt

                    is_active = bool(r.get("isActive"))
                    is_frozen = bool(r.get("isFrozen"))
                    is_paused = bool(r.get("isPaused"))
                    is_siloed = bool(r.get("isSiloedBorrowing"))

                    liquidity_rate_ray = int(r.get("liquidityRate") or 0)
                    variable_borrow_rate_ray = int(r.get("variableBorrowRate") or 0)

                    price_market_ref = int(r.get("priceInMarketReferenceCurrency") or 0)
                    try:
                        price_market_ref_float = (
                            float(price_market_ref) / ref_unit if ref_unit else 0.0
                        )
                    except ZeroDivisionError:
                        price_market_ref_float = 0.0
                    price_usd = price_market_ref_float * ref_usd

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

                    markets.append(
                        {
                            "underlying": underlying,
                            "symbol": str(symbol_raw),
                            "symbol_canonical": symbol_canonical,
                            "decimals": int(decimals),
                            "a_token": a_token,
                            "variable_debt_token": v_debt,
                            "is_active": is_active,
                            "is_frozen": is_frozen,
                            "is_paused": is_paused,
                            "is_siloed_borrowing": is_siloed,
                            "is_stablecoin": is_stable_symbol(symbol_raw),
                            "usage_as_collateral_enabled": bool(
                                r.get("usageAsCollateralEnabled")
                            ),
                            "ltv_bps": int(r.get("baseLTVasCollateral") or 0),
                            "liquidation_threshold_bps": int(
                                r.get("reserveLiquidationThreshold") or 0
                            ),
                            "liquidation_bonus_bps": int(
                                r.get("reserveLiquidationBonus") or 0
                            ),
                            "reserve_factor_bps": int(r.get("reserveFactor") or 0),
                            "borrowing_enabled": bool(r.get("borrowingEnabled")),
                            "borrow_cap": int(r.get("borrowCap") or 0),
                            "price_usd": float(price_usd),
                            "supply_apr": float(supply_apr),
                            "supply_apy": float(apr_to_apy(supply_apr)),
                            "variable_borrow_apr": float(borrow_apr),
                            "variable_borrow_apy": float(apr_to_apy(borrow_apr)),
                            "available_liquidity": int(available_liquidity),
                            "total_variable_debt": int(total_variable_debt),
                            "tvl": int(tvl),
                            "supply_cap": supply_cap_tokens,
                            "supply_cap_headroom": headroom_wei,
                            "debt_ceiling": int(r.get("debtCeiling") or 0),
                            "debt_ceiling_decimals": int(
                                r.get("debtCeilingDecimals") or 0
                            ),
                        }
                    )

                return True, markets
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def lend(
        self,
        *,
        underlying_token: str,
        qty: int,
        chain_id: int,
        native: bool = False,
        strategy_name: str | None = None,
    ) -> tuple[bool, Any]:
        strategy = self.wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"
        if qty <= 0:
            return False, "qty must be positive"

        if native:
            token_addr = HYPEREVM_WHYPE
            transaction = await encode_call(
                target=HYPERLEND_WRAPPED_TOKEN_GATEWAY,
                abi=WRAPPED_TOKEN_GATEWAY_ABI,
                fn_name="depositETH",
                args=[HYPEREVM_WHYPE, strategy, REFERRAL_CODE],
                from_address=strategy,
                chain_id=chain_id,
                value=qty,
            )
        else:
            token_addr = to_checksum_address(underlying_token)
            approved = await ensure_allowance(
                token_address=token_addr,
                owner=strategy,
                spender=HYPERLEND_POOL,
                amount=qty,
                chain_id=chain_id,
                signing_callback=self.signing_callback,
            )
            if not approved[0]:
                return approved
            transaction = await encode_call(
                target=HYPERLEND_POOL,
                abi=POOL_ABI,
                fn_name="supply",
                args=[token_addr, qty, strategy, REFERRAL_CODE],
                from_address=strategy,
                chain_id=chain_id,
            )

        txn_hash = await send_transaction(transaction, self.signing_callback)

        await self._record_pool_op(
            token_address=token_addr,
            amount=qty,
            chain_id=chain_id,
            wallet_address=strategy,
            txn_hash=txn_hash,
            strategy_name=strategy_name,
            op_type="lend",
        )

        return (True, txn_hash)

    async def unlend(
        self,
        *,
        underlying_token: str,
        qty: int,
        chain_id: int,
        native: bool = False,
        strategy_name: str | None = None,
    ) -> tuple[bool, Any]:
        strategy = self.wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"
        if qty <= 0:
            return False, "qty must be positive"

        if native:
            token_addr = HYPEREVM_WHYPE
            transaction = await encode_call(
                target=HYPERLEND_WRAPPED_TOKEN_GATEWAY,
                abi=WRAPPED_TOKEN_GATEWAY_ABI,
                fn_name="withdrawETH",
                args=[HYPEREVM_WHYPE, qty, strategy],
                from_address=strategy,
                chain_id=chain_id,
            )
        else:
            token_addr = to_checksum_address(underlying_token)
            transaction = await encode_call(
                target=HYPERLEND_POOL,
                abi=POOL_ABI,
                fn_name="withdraw",
                args=[token_addr, qty, strategy],
                from_address=strategy,
                chain_id=chain_id,
            )

        txn_hash = await send_transaction(transaction, self.signing_callback)
        await self._record_pool_op(
            token_address=token_addr,
            amount=qty,
            chain_id=chain_id,
            wallet_address=strategy,
            txn_hash=txn_hash,
            strategy_name=strategy_name,
            op_type="unlend",
        )

        return True, txn_hash

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
        if qty <= 0:
            return False, "qty must be positive"

        if native:
            asset = HYPEREVM_WHYPE
            borrow_tx = await encode_call(
                target=HYPERLEND_POOL,
                abi=POOL_ABI,
                fn_name="borrow",
                args=[asset, qty, VARIABLE_RATE_MODE, REFERRAL_CODE, strategy],
                from_address=strategy,
                chain_id=chain_id,
            )
            borrow_tx_hash = await send_transaction(borrow_tx, self.signing_callback)

            unwrap_tx = await encode_call(
                target=asset,
                abi=WETH_ABI,
                fn_name="withdraw",
                args=[qty],
                from_address=strategy,
                chain_id=chain_id,
            )
            unwrap_tx_hash = await send_transaction(unwrap_tx, self.signing_callback)
            return True, {"borrow_tx": borrow_tx_hash, "unwrap_tx": unwrap_tx_hash}
        else:
            asset = to_checksum_address(underlying_token)
            transaction = await encode_call(
                target=HYPERLEND_POOL,
                abi=POOL_ABI,
                fn_name="borrow",
                args=[asset, qty, VARIABLE_RATE_MODE, REFERRAL_CODE, strategy],
                from_address=strategy,
                chain_id=chain_id,
            )

        txn_hash = await send_transaction(transaction, self.signing_callback)
        return True, txn_hash

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
        if qty <= 0 and not repay_full:
            return False, "qty must be positive"

        if native:
            if repay_full:
                async with web3_from_chain_id(chain_id) as web3:
                    cache_key = HYPEREVM_WHYPE.lower()
                    variable_debt_token = self._variable_debt_token_by_underlying.get(
                        cache_key
                    )
                    if not variable_debt_token:
                        ui_pool = web3.eth.contract(
                            address=HYPERLEND_UI_POOL_DATA_PROVIDER,
                            abi=UI_POOL_DATA_PROVIDER_ABI,
                        )
                        reserves, _ = await ui_pool.functions.getReservesData(
                            HYPERLEND_POOL_ADDRESSES_PROVIDER
                        ).call(block_identifier="pending")

                        reserve_keys = UI_POOL_RESERVE_KEYS
                        for reserve in reserves or []:
                            r = _reserve_to_dict(reserve, reserve_keys)
                            underlying = str(r.get("underlyingAsset") or "")
                            if underlying and underlying.lower() == cache_key:
                                v = r.get("variableDebtTokenAddress")
                                if v:
                                    variable_debt_token = to_checksum_address(str(v))
                                break

                        if not variable_debt_token:
                            return (
                                False,
                                "could not resolve variable debt token for WHYPE",
                            )

                        self._variable_debt_token_by_underlying[cache_key] = (
                            variable_debt_token
                        )

                    variable_debt = await get_token_balance(
                        str(variable_debt_token),
                        chain_id,
                        strategy,
                        web3=web3,
                        block_identifier="pending",
                    )
                    if variable_debt <= 0:
                        return True, None

                    native_balance = await get_token_balance(
                        None,
                        chain_id,
                        strategy,
                        web3=web3,
                        block_identifier="pending",
                    )

                    # Send a small buffer to avoid leaving dust from interest accrual between
                    # the read and execution; excess is expected to be refunded by the gateway.
                    buffer_wei = max(1, variable_debt // 10_000)  # 0.01%
                    value = variable_debt + buffer_wei
                    if native_balance < value:
                        if native_balance < variable_debt:
                            return (
                                False,
                                f"insufficient HYPE balance for repay_full (debt_wei={variable_debt}, balance_wei={native_balance})",
                            )
                        value = variable_debt

                transaction = await encode_call(
                    target=HYPERLEND_WRAPPED_TOKEN_GATEWAY,
                    abi=WRAPPED_TOKEN_GATEWAY_ABI,
                    fn_name="repayETH",
                    args=[HYPEREVM_WHYPE, MAX_UINT256, strategy],
                    from_address=strategy,
                    chain_id=chain_id,
                    value=value,
                )
            else:
                transaction = await encode_call(
                    target=HYPERLEND_WRAPPED_TOKEN_GATEWAY,
                    abi=WRAPPED_TOKEN_GATEWAY_ABI,
                    fn_name="repayETH",
                    args=[HYPEREVM_WHYPE, qty, strategy],
                    from_address=strategy,
                    chain_id=chain_id,
                    value=qty,
                )

        else:
            repay_amount = MAX_UINT256 if repay_full else qty
            asset = to_checksum_address(underlying_token)
            allowance_target = MAX_UINT256 if repay_full else qty
            approved = await ensure_allowance(
                token_address=asset,
                owner=strategy,
                spender=HYPERLEND_POOL,
                amount=allowance_target,
                chain_id=chain_id,
                signing_callback=self.signing_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved[0]:
                return approved

            transaction = await encode_call(
                target=HYPERLEND_POOL,
                abi=POOL_ABI,
                fn_name="repay",
                args=[asset, repay_amount, VARIABLE_RATE_MODE, strategy],
                from_address=strategy,
                chain_id=chain_id,
            )

        txn_hash = await send_transaction(transaction, self.signing_callback)
        return True, txn_hash

    async def set_collateral(
        self,
        *,
        underlying_token: str,
        chain_id: int,
    ) -> tuple[bool, Any]:
        strategy = self.wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"

        asset = to_checksum_address(underlying_token)
        transaction = await encode_call(
            target=HYPERLEND_POOL,
            abi=POOL_ABI,
            fn_name="setUserUseReserveAsCollateral",
            args=[asset, True],
            from_address=strategy,
            chain_id=chain_id,
        )
        txn_hash = await send_transaction(transaction, self.signing_callback)
        return True, txn_hash

    async def remove_collateral(
        self,
        *,
        underlying_token: str,
        chain_id: int,
    ) -> tuple[bool, Any]:
        strategy = self.wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"

        asset = to_checksum_address(underlying_token)
        transaction = await encode_call(
            target=HYPERLEND_POOL,
            abi=POOL_ABI,
            fn_name="setUserUseReserveAsCollateral",
            args=[asset, False],
            from_address=strategy,
            chain_id=chain_id,
        )
        txn_hash = await send_transaction(transaction, self.signing_callback)
        return True, txn_hash

    async def _record_pool_op(
        self,
        token_address: str,
        amount: int,
        chain_id: int,
        wallet_address: str,
        txn_hash: str,
        op_type: Literal["lend", "unlend"],
        strategy_name: str | None = None,
    ):
        amount_usd = await self._calculate_amount_usd(
            token_address=token_address,
            amount=amount,
            chain_id=chain_id,
        )

        model = {"lend": LEND, "unlend": UNLEND}[op_type]

        operation_data = model(
            adapter=self.adapter_type,
            token_address=token_address,
            pool_address=HYPERLEND_POOL,
            amount=str(amount),
            amount_usd=amount_usd or 0,
            transaction_hash=txn_hash,
            transaction_chain_id=chain_id,
        )

        success, ledger_response = await self.ledger_adapter.record_operation(
            wallet_address=wallet_address,
            operation_data=operation_data,
            usd_value=amount_usd or 0,
            strategy_name=strategy_name,
        )
        if not success:
            self.logger.warning("Ledger record failed", error=ledger_response)

    async def _calculate_amount_usd(
        self,
        token_address: str,
        amount: int,
        chain_id: int,
    ) -> float | None:
        success, token_data = await self.token_adapter.get_token(
            query=token_address,
            chain_id=chain_id,
        )
        if not success or not token_data:
            self.logger.warning(
                f"Could not get token info for {token_address} on chain {chain_id}"
            )
            return None

        decimals, current_price = (
            token_data["decimals"],
            token_data["current_price"],
        )

        if decimals is None or current_price is None:
            self.logger.warning(
                f"Could not get decimal or current_price info for {token_address} on chain {chain_id}"
            )
            return None

        return float(current_price) * float(amount) / 10 ** int(decimals)
