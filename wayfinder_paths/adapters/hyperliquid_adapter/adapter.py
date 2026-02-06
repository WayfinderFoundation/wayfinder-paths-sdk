from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from decimal import ROUND_DOWN, Decimal, getcontext
from typing import Any

from aiocache import Cache
from eth_utils import to_checksum_address
from hyperliquid.utils.types import BuilderInfo
from loguru import logger

from wayfinder_paths.adapters.hyperliquid_adapter.exchange import Exchange
from wayfinder_paths.adapters.hyperliquid_adapter.info import get_info
from wayfinder_paths.adapters.hyperliquid_adapter.local_signer import (
    create_local_signer,
)
from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter
from wayfinder_paths.core.constants import ZERO_ADDRESS
from wayfinder_paths.core.constants.contracts import HYPERCORE_SENTINEL_ADDRESS
from wayfinder_paths.core.constants.hyperliquid import (
    DEFAULT_HYPERLIQUID_BUILDER_FEE_TENTHS_BP,
    HYPE_FEE_WALLET,
)


class HyperliquidAdapter(BaseAdapter):
    adapter_type = "HYPERLIQUID"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        sign_callback: Callable[[dict], Awaitable[str]] | None = None,
    ) -> None:
        super().__init__("hyperliquid_adapter", config)

        self._cache = Cache(Cache.MEMORY)
        self._sign_callback = sign_callback
        self._exchange: Exchange | None = None

    @property
    def exchange(self) -> Exchange:
        """Lazily initialize the Exchange for write operations."""
        if self._exchange is None:
            if self._sign_callback is None:
                if not self.config:
                    raise ValueError(
                        "Config required for local signing (no sign_callback provided)"
                    )
                sign_callback = create_local_signer(self.config)
                signing_type = "local"
            else:
                sign_callback = self._sign_callback
                signing_type = "eip712"

            self._exchange = Exchange(
                sign_callback=sign_callback,
                signing_type=signing_type,
            )
        return self._exchange

    async def get_meta_and_asset_ctxs(self) -> tuple[bool, Any]:
        cache_key = "hl_meta_and_asset_ctxs"
        cached = await self._cache.get(cache_key)
        if cached:
            return True, cached

        try:
            data = get_info().meta_and_asset_ctxs()
            await self._cache.set(cache_key, data, ttl=60)
            return True, data
        except Exception as exc:
            self.logger.error(f"Failed to fetch meta_and_asset_ctxs: {exc}")
            return False, str(exc)

    async def get_spot_meta(self) -> tuple[bool, Any]:
        cache_key = "hl_spot_meta"
        cached = await self._cache.get(cache_key)
        if cached:
            return True, cached

        try:
            # Handle both callable and property access patterns
            spot_meta = get_info().spot_meta
            if callable(spot_meta):
                data = spot_meta()
            else:
                data = spot_meta
            await self._cache.set(cache_key, data, ttl=60)
            return True, data
        except Exception as exc:
            self.logger.error(f"Failed to fetch spot_meta: {exc}")
            return False, str(exc)

    @staticmethod
    def max_transferable_amount(
        total: str,
        hold: str,
        *,
        sz_decimals: int,
        leave_one_tick: bool = True,
    ) -> float:
        """Compute max transferable: (total - hold) rounded down, leaving 1 tick margin."""
        getcontext().prec = 50

        if sz_decimals < 0:
            sz_decimals = 0

        step = Decimal(10) ** (-int(sz_decimals))

        total_d = Decimal(str(total or "0"))
        hold_d = Decimal(str(hold or "0"))
        available = total_d - hold_d
        if available <= 0:
            return 0.0

        safe = available - step if leave_one_tick else available
        if safe <= 0:
            return 0.0

        quantized = (safe / step).to_integral_value(rounding=ROUND_DOWN) * step
        if quantized <= 0:
            return 0.0
        return float(quantized)

    async def get_spot_assets(self) -> tuple[bool, dict[str, int]]:
        cache_key = "hl_spot_assets"
        cached = await self._cache.get(cache_key)
        if cached:
            return True, cached

        try:
            success, spot_meta = await self.get_spot_meta()
            if not success:
                return False, {}

            response = {}
            tokens = spot_meta.get("tokens", [])
            universe = spot_meta.get("universe", [])

            for pair in universe:
                pair_tokens = pair.get("tokens", [])
                if len(pair_tokens) < 2:
                    continue

                base_idx, quote_idx = pair_tokens[0], pair_tokens[1]

                base_info = tokens[base_idx] if base_idx < len(tokens) else {}
                quote_info = tokens[quote_idx] if quote_idx < len(tokens) else {}

                base_name = base_info.get("name", f"TOKEN{base_idx}")
                quote_name = quote_info.get("name", f"TOKEN{quote_idx}")

                name = f"{base_name}/{quote_name}"
                spot_asset_id = pair.get("index", 0) + 10000
                response[name] = spot_asset_id

            await self._cache.set(cache_key, response, ttl=300)
            return True, response

        except Exception as exc:
            self.logger.error(f"Failed to get spot assets: {exc}")
            return False, {}

    async def get_spot_asset_id(
        self, base_coin: str, quote_coin: str = "USDC"
    ) -> int | None:
        cache_key = "hl_spot_assets"
        cached = await self._cache.get(cache_key)
        if cached:
            pair_name = f"{base_coin}/{quote_coin}"
            return cached.get(pair_name)
        return None

    async def get_l2_book(
        self,
        coin: str,
        n_levels: int = 20,
    ) -> tuple[bool, dict[str, Any]]:
        try:
            data = get_info().l2_snapshot(coin)
            return True, data
        except Exception as exc:
            self.logger.error(f"Failed to fetch L2 book for {coin}: {exc}")
            return False, str(exc)

    async def get_user_state(self, address: str) -> tuple[bool, dict[str, Any]]:
        try:
            data = get_info().user_state(address)
            return True, data
        except Exception as exc:
            self.logger.error(f"Failed to fetch user_state for {address}: {exc}")
            return False, str(exc)

    async def get_spot_user_state(self, address: str) -> tuple[bool, dict[str, Any]]:
        try:
            data = get_info().spot_user_state(address)
            return True, data
        except Exception as exc:
            self.logger.error(f"Failed to fetch spot_user_state for {address}: {exc}")
            return False, str(exc)

    async def get_full_user_state(
        self,
        *,
        account: str,
        include_spot: bool = True,
        include_open_orders: bool = True,
        include_frontend_open_orders: bool = True,
    ) -> tuple[bool, dict[str, Any] | str]:
        out: dict[str, Any] = {
            "protocol": "hyperliquid",
            "account": account,
            "perp": None,
            "spot": None,
            "openOrders": None,
            "errors": {},
        }

        ok_any = False

        ok_perp, perp = await self.get_user_state(account)
        if ok_perp:
            ok_any = True
            out["perp"] = perp
            out["positions"] = perp.get("assetPositions", [])
        else:
            out["errors"]["perp"] = perp

        if include_spot:
            ok_spot, spot = await self.get_spot_user_state(account)
            if ok_spot:
                ok_any = True
                out["spot"] = spot
            else:
                out["errors"]["spot"] = spot

        if include_open_orders:
            if include_frontend_open_orders:
                ok_orders, orders = await self.get_frontend_open_orders(account)
            else:
                ok_orders, orders = await self.get_open_orders(account)
            if ok_orders:
                ok_any = True
                out["openOrders"] = orders
            else:
                out["errors"]["openOrders"] = orders

        return ok_any, out

    async def get_margin_table(self, margin_table_id: int) -> tuple[bool, list[dict]]:
        cache_key = f"hl_margin_table_{margin_table_id}"
        cached = await self._cache.get(cache_key)
        if cached:
            return True, cached

        try:
            # Hyperliquid expects `id` but older SDKs may use `marginTableId`
            body = {"type": "marginTable", "id": int(margin_table_id)}
            try:
                data = get_info().post("/info", body)
            except Exception:  # noqa: BLE001
                body = {"type": "marginTable", "marginTableId": int(margin_table_id)}
                data = get_info().post("/info", body)
            await self._cache.set(cache_key, data, ttl=86400)
            return True, data
        except Exception as exc:
            self.logger.error(f"Failed to fetch margin_table {margin_table_id}: {exc}")
            return False, str(exc)

    async def get_spot_l2_book(self, spot_asset_id: int) -> tuple[bool, dict[str, Any]]:
        try:
            spot_index = (
                spot_asset_id - 10000 if spot_asset_id >= 10000 else spot_asset_id
            )
            # Index 0 (PURR) uses pair name; others use @{index}
            coin = "PURR/USDC" if spot_index == 0 else f"@{spot_index}"
            data = get_info().l2_snapshot(coin)
            return True, data
        except Exception as exc:
            self.logger.error(
                f"Failed to fetch spot L2 book for {spot_asset_id}: {exc}"
            )
            return False, str(exc)

    @property
    def asset_to_sz_decimals(self) -> dict[int, int]:
        return get_info().asset_to_sz_decimals

    @property
    def coin_to_asset(self) -> dict[str, int]:
        return get_info().coin_to_asset

    def get_sz_decimals(self, asset_id: int) -> int:
        try:
            return self.asset_to_sz_decimals[asset_id]
        except KeyError:
            raise ValueError(
                f"Unknown asset_id {asset_id}: missing szDecimals"
            ) from None

    async def get_all_mid_prices(self) -> tuple[bool, dict[str, float]]:
        try:
            data = get_info().all_mids()
            return True, {k: float(v) for k, v in data.items()}
        except Exception as exc:
            self.logger.error(f"Failed to fetch mid prices: {exc}")
            return False, str(exc)

    def get_valid_order_size(self, asset_id: int, size: float) -> float:
        decimals = self.get_sz_decimals(asset_id)
        step = Decimal(10) ** (-decimals)
        if size <= 0:
            return 0.0
        quantized = (Decimal(str(size)) / step).to_integral_value(
            rounding=ROUND_DOWN
        ) * step
        return float(quantized)

    def _mandatory_builder_fee(self, builder: dict[str, Any] | None) -> dict[str, Any]:
        expected_builder = HYPE_FEE_WALLET.lower()

        if isinstance(builder, dict) and builder.get("b") is not None:
            provided_builder = str(builder.get("b") or "").strip()
            if provided_builder and provided_builder.lower() != expected_builder:
                raise ValueError(
                    f"builder wallet must be {expected_builder} (got {provided_builder})"
                )

        fee = None
        if isinstance(builder, dict) and builder.get("f") is not None:
            fee = builder.get("f")

        if fee is None and isinstance(self.config, dict):
            cfg = self.config.get("builder_fee")
            if isinstance(cfg, dict):
                cfg_builder = str(cfg.get("b") or "").strip()
                if cfg_builder and cfg_builder.lower() != expected_builder:
                    raise ValueError(
                        f"config builder_fee.b must be {expected_builder} (got {cfg_builder})"
                    )
                if cfg.get("f") is not None:
                    fee = cfg.get("f")

        if fee is None:
            fee = DEFAULT_HYPERLIQUID_BUILDER_FEE_TENTHS_BP

        try:
            fee_i = int(fee)
        except (TypeError, ValueError) as exc:
            raise ValueError("builder fee f must be an int (tenths of bp)") from exc
        if fee_i <= 0:
            raise ValueError("builder fee f must be > 0 (tenths of bp)")

        return {"b": expected_builder, "f": fee_i}

    async def place_market_order(
        self,
        asset_id: int,
        is_buy: bool,
        slippage: float,
        size: float,
        address: str,
        *,
        reduce_only: bool = False,
        cloid: str | None = None,
        builder: dict[str, Any] | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        builder = self._mandatory_builder_fee(builder)
        result = await self.exchange.place_market_order(
            asset_id=asset_id,
            is_buy=is_buy,
            slippage=slippage,
            size=size,
            address=address,
            reduce_only=reduce_only,
            cloid=cloid,
            builder=BuilderInfo(b=builder.get("b"), f=builder.get("f")),
        )

        success = result.get("status") == "ok"
        if success:
            statuses = result.get("response", {}).get("data", {}).get("statuses", [])
            success = not any(isinstance(s, dict) and s.get("error") for s in statuses)
        return success, result

    async def cancel_order(
        self,
        asset_id: int,
        order_id: int | str,
        address: str,
    ) -> tuple[bool, dict[str, Any]]:
        try:
            order_id_int = int(order_id)
        except (TypeError, ValueError):
            return (
                False,
                {
                    "status": "err",
                    "response": {
                        "type": "error",
                        "data": f"Invalid order_id for cancel_order: {order_id}",
                    },
                },
            )

        result = await self.exchange.cancel_order(
            asset_id=asset_id,
            order_id=order_id_int,
            address=address,
        )

        success = result.get("status") == "ok"
        return success, result

    async def cancel_order_by_cloid(
        self,
        asset_id: int,
        cloid: str,
        address: str,
    ) -> tuple[bool, dict[str, Any]]:
        """Cancel order by client order ID (looks up oid from open orders first)."""
        success, orders = await self.get_frontend_open_orders(address)
        if not success:
            return False, {
                "status": "err",
                "response": {"type": "error", "data": "Could not fetch open orders"},
            }

        matching_order = next((o for o in orders if o.get("cloid") == cloid), None)

        if not matching_order:
            return False, {
                "status": "err",
                "response": {
                    "type": "error",
                    "data": f"Order with cloid {cloid} not found",
                },
            }

        order_id = matching_order.get("oid")
        if not order_id:
            return False, {
                "status": "err",
                "response": {"type": "error", "data": "Order missing oid"},
            }

        return await self.cancel_order(
            asset_id=asset_id, order_id=order_id, address=address
        )

    async def spot_transfer(
        self,
        *,
        amount: float,
        destination: str,
        token: str,
        address: str,
    ) -> tuple[bool, dict[str, Any]]:
        result = await self.exchange.spot_transfer(
            signature_chain_id=42161,  # Arbitrum
            destination=str(destination),
            token=str(token),
            amount=str(amount),
            address=address,
        )

        success = result.get("status") == "ok"
        return success, result

    @staticmethod
    def hypercore_index_to_system_address(index: int) -> str:
        if index == 150:
            return HYPERCORE_SENTINEL_ADDRESS

        hex_index = f"{index:x}"
        padding_length = 42 - len("0x20") - len(hex_index)
        result = "0x20" + "0" * padding_length + hex_index
        return to_checksum_address(result)

    async def hypercore_get_token_metadata(
        self, token_address: str | None
    ) -> dict[str, Any] | None:
        """Resolve spot token metadata by EVM address (0-address → HYPE at index 150)."""
        token_addr = (token_address or ZERO_ADDRESS).strip()
        token_addr_lower = token_addr.lower()

        success, spot_meta = await self.get_spot_meta()
        if not success or not isinstance(spot_meta, dict):
            return None

        tokens = spot_meta.get("tokens", [])
        if not isinstance(tokens, list) or not tokens:
            return None

        if token_addr_lower == ZERO_ADDRESS.lower():
            return tokens[150] if len(tokens) > 150 else None

        for token_data in tokens:
            if not isinstance(token_data, dict):
                continue
            evm_contract = token_data.get("evmContract")
            if not isinstance(evm_contract, dict):
                continue
            address = evm_contract.get("address")
            if isinstance(address, str) and address.lower() == token_addr_lower:
                return token_data

        return None

    async def hypercore_to_hyperevm(
        self,
        *,
        amount: float,
        address: str,
        token_address: str | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Transfer spot token from HyperCore to HyperEVM (destination is system address, not wallet)."""
        token_data = await self.hypercore_get_token_metadata(token_address)
        if not token_data:
            return False, {
                "status": "err",
                "response": {"type": "error", "data": "Token not found in spot meta"},
            }

        try:
            index = int(token_data.get("index"))
        except (TypeError, ValueError):
            return False, {
                "status": "err",
                "response": {"type": "error", "data": "Token metadata missing index"},
            }

        destination = self.hypercore_index_to_system_address(index)
        name = token_data.get("name")
        token_id = token_data.get("tokenId")
        if not isinstance(name, str) or not name:
            return False, {
                "status": "err",
                "response": {"type": "error", "data": "Token metadata missing name"},
            }
        if token_id is None:
            return False, {
                "status": "err",
                "response": {"type": "error", "data": "Token metadata missing tokenId"},
            }
        token_string = f"{name}:{token_id}"

        return await self.spot_transfer(
            amount=float(amount),
            destination=destination,
            token=token_string,
            address=address,
        )

    async def update_leverage(
        self,
        asset_id: int,
        leverage: int,
        is_cross: bool,
        address: str,
    ) -> tuple[bool, dict[str, Any]]:
        result = await self.exchange.update_leverage(
            asset=asset_id,
            leverage=leverage,
            is_cross=is_cross,
            address=address,
        )

        success = result.get("status") == "ok"
        return success, result

    async def transfer_spot_to_perp(
        self,
        amount: float,
        address: str,
    ) -> tuple[bool, dict[str, Any]]:
        result = await self.exchange.usd_class_transfer(
            amount=amount,
            address=address,
            to_perp=True,
        )

        success = result.get("status") == "ok"
        return success, result

    async def transfer_perp_to_spot(
        self,
        amount: float,
        address: str,
    ) -> tuple[bool, dict[str, Any]]:
        result = await self.exchange.usd_class_transfer(
            amount=amount,
            address=address,
            to_perp=False,
        )

        success = result.get("status") == "ok"
        return success, result

    async def place_stop_loss(
        self,
        asset_id: int,
        is_buy: bool,
        trigger_price: float,
        size: float,
        address: str,
    ) -> tuple[bool, dict[str, Any]]:
        result = await self.exchange.place_trigger_order(
            asset_id=asset_id,
            is_buy=is_buy,
            trigger_price=trigger_price,
            size=size,
            address=address,
            tpsl="sl",
            is_market=True,
        )

        success = result.get("status") == "ok"
        return success, result

    async def get_user_fills(self, address: str) -> tuple[bool, list[dict[str, Any]]]:
        try:
            data = get_info().user_fills(address)
            return True, data if isinstance(data, list) else []
        except Exception as exc:
            self.logger.error(f"Failed to fetch user_fills for {address}: {exc}")
            return False, str(exc)

    async def check_recent_liquidations(
        self, address: str, since_ms: int
    ) -> tuple[bool, list[dict[str, Any]]]:
        try:
            now_ms = int(time.time() * 1000)
            data = get_info().user_fills_by_time(address, since_ms, now_ms)
            fills = data if isinstance(data, list) else []

            # Filter for liquidation fills where we were the liquidated user
            liquidation_fills = [
                f
                for f in fills
                if f.get("liquidation")
                and f["liquidation"].get("liquidatedUser", "").lower()
                == address.lower()
            ]

            return True, liquidation_fills
        except Exception as exc:
            self.logger.error(f"Failed to check liquidations for {address}: {exc}")
            return False, []

    async def get_order_status(
        self, address: str, order_id: int | str
    ) -> tuple[bool, dict[str, Any]]:
        try:
            data = get_info().query_order_by_oid(address, int(order_id))
            return True, data
        except Exception as exc:
            self.logger.error(f"Failed to fetch order_status for {order_id}: {exc}")
            return False, str(exc)

    async def get_open_orders(self, address: str) -> tuple[bool, list[dict[str, Any]]]:
        try:
            data = get_info().open_orders(address)
            return True, data if isinstance(data, list) else []
        except Exception as exc:
            self.logger.error(f"Failed to fetch open_orders for {address}: {exc}")
            return False, str(exc)

    async def get_frontend_open_orders(
        self, address: str
    ) -> tuple[bool, list[dict[str, Any]]]:
        try:
            data = get_info().frontend_open_orders(address)
            return True, data if isinstance(data, list) else []
        except Exception as exc:
            self.logger.error(
                f"Failed to fetch frontend_open_orders for {address}: {exc}"
            )
            return False, str(exc)

    async def withdraw(
        self,
        *,
        amount: float,
        address: str,
    ) -> tuple[bool, dict[str, Any]]:
        result = await self.exchange.withdraw(
            amount=amount,
            address=address,
        )
        success = result.get("status") == "ok"
        return success, result

    def get_perp_margin_amount(self, user_state: dict[str, Any]) -> float:
        try:
            margin_summary = user_state.get("marginSummary", {})
            account_value = margin_summary.get("accountValue")
            if account_value is not None:
                return float(account_value)
            cross_summary = user_state.get("crossMarginSummary", {})
            return float(cross_summary.get("accountValue", 0.0))
        except (TypeError, ValueError):
            return 0.0

    async def get_max_builder_fee(
        self,
        user: str,
        builder: str,
    ) -> tuple[bool, int]:
        try:
            body = {"type": "maxBuilderFee", "user": user, "builder": builder}
            data = get_info().post("/info", body)
            # Response is just an integer (tenths of basis points)
            return True, int(data) if data is not None else 0
        except Exception as exc:
            self.logger.error(f"Failed to fetch max_builder_fee for {user}: {exc}")
            return False, 0

    async def approve_builder_fee(
        self,
        builder: str,
        max_fee_rate: str,
        address: str,
    ) -> tuple[bool, dict[str, Any]]:
        result = await self.exchange.approve_builder_fee(
            builder=builder,
            max_fee_rate=max_fee_rate,
            address=address,
        )

        success = result.get("status") == "ok"
        return success, result

    async def ensure_builder_fee_approved(
        self,
        address: str,
        builder_fee: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        # Resolve fee config from parameter or config
        fee_config = builder_fee
        if not fee_config and isinstance(self.config, dict):
            fee_config = self.config.get("builder_fee")

        if not fee_config or not isinstance(fee_config, dict):
            return True, "No builder fee configured"

        builder = fee_config.get("b")
        required_fee = fee_config.get("f", 0)
        if not builder or not required_fee:
            return True, "Builder fee not configured"

        # Check current approval
        try:
            ok, current_fee = await self.get_max_builder_fee(address, builder)
            if ok and int(current_fee) >= int(required_fee):
                return (
                    True,
                    f"Builder fee already approved ({current_fee} >= {required_fee})",
                )
        except Exception as e:
            logger.warning(
                f"Failed to check builder fee: {e}, proceeding with approval"
            )

        # Approve
        max_fee_rate = f"{int(required_fee) / 1000:.3f}%"
        ok, result = await self.approve_builder_fee(builder, max_fee_rate, address)
        if ok:
            return True, f"Builder fee approved: {max_fee_rate}"
        return False, f"Builder fee approval failed: {result}"

    async def place_limit_order(
        self,
        asset_id: int,
        is_buy: bool,
        price: float,
        size: float,
        address: str,
        *,
        reduce_only: bool = False,
        builder: dict[str, Any] | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        builder = self._mandatory_builder_fee(builder)
        result = await self.exchange.place_limit_order(
            asset_id=asset_id,
            is_buy=is_buy,
            price=price,
            size=size,
            address=address,
            builder=BuilderInfo(b=builder.get("b"), f=builder.get("f")),
        )

        success = result.get("status") == "ok"
        return success, result

    async def wait_for_deposit(
        self,
        address: str,
        expected_increase: float,
        *,
        timeout_s: int = 120,
        poll_interval_s: int = 5,
    ) -> tuple[bool, float]:
        timeout_s = max(0, int(timeout_s))
        poll_interval_s = max(1, int(poll_interval_s))
        iterations = int(timeout_s // poll_interval_s) + 1

        success, initial_state = await self.get_user_state(address)
        if not success:
            self.logger.warning(f"Could not fetch initial state: {initial_state}")
            initial_balance = 0.0
        else:
            initial_balance = self.get_perp_margin_amount(initial_state)

        self.logger.info(
            f"Waiting for Hyperliquid deposit. Initial balance: ${initial_balance:.2f}, "
            f"expecting +${expected_increase:.2f}"
        )

        # Also check ledger in case deposit already credited before this call
        started_ms = int(time.time() * 1000)
        from_timestamp_ms = started_ms - (timeout_s * 1000)
        expected_min = float(expected_increase) * 0.95

        ok_ledger, deposits = await self.get_user_deposits(address, from_timestamp_ms)
        if ok_ledger and any(float(v or 0) >= expected_min for v in deposits.values()):
            self.logger.info("Hyperliquid deposit confirmed via ledger updates.")
            return True, float(initial_balance)

        for i in range(iterations):
            if i > 0:
                await asyncio.sleep(poll_interval_s)

            success, state = await self.get_user_state(address)
            if not success:
                continue

            current_balance = self.get_perp_margin_amount(state)

            # Allow 5% tolerance for fees/slippage
            if current_balance >= initial_balance + expected_min:
                self.logger.info(
                    f"Hyperliquid deposit confirmed: ${current_balance - initial_balance:.2f} "
                    f"(expected ${expected_increase:.2f})"
                )
                return True, current_balance

            ok_ledger, deposits = await self.get_user_deposits(
                address, from_timestamp_ms
            )
            if ok_ledger and any(
                float(v or 0) >= expected_min for v in deposits.values()
            ):
                self.logger.info("Hyperliquid deposit confirmed via ledger updates.")
                return True, float(current_balance)

            remaining_s = (iterations - i - 1) * poll_interval_s
            self.logger.debug(
                f"Waiting for deposit... current=${current_balance:.2f}, "
                f"need=${initial_balance + expected_increase:.2f}, {remaining_s}s remaining"
            )

        self.logger.warning(
            f"Hyperliquid deposit not confirmed after {timeout_s}s. "
            "Deposits typically credit in < 1 minute (but can take longer)."
        )
        success, state = await self.get_user_state(address)
        final_balance = (
            self.get_perp_margin_amount(state) if success else initial_balance
        )
        return False, final_balance

    async def get_user_deposits(
        self,
        address: str,
        from_timestamp_ms: int,
    ) -> tuple[bool, dict[str, float]]:
        try:
            data = get_info().user_non_funding_ledger_updates(
                to_checksum_address(address), int(from_timestamp_ms)
            )
            result: dict[str, float] = {}
            for update in sorted(data or [], key=lambda x: x.get("time", 0)):
                delta = update.get("delta") or {}
                if delta.get("type") == "deposit":
                    tx_hash = (
                        update.get("hash")
                        or update.get("txHash")
                        or update.get("tx_hash")
                        or update.get("transactionHash")
                    )
                    usdc_amount = float(delta.get("usdc", 0))
                    if not tx_hash:
                        ts = int(update.get("time") or 0)
                        tx_hash = f"deposit-{ts}-{len(result)}"
                    result[str(tx_hash)] = usdc_amount

            return True, result

        except Exception as exc:
            self.logger.error(f"Failed to get user deposits: {exc}")
            return False, {}

    async def get_user_withdrawals(
        self,
        address: str,
        from_timestamp_ms: int,
    ) -> tuple[bool, dict[str, float]]:
        try:
            data = get_info().user_non_funding_ledger_updates(
                to_checksum_address(address), int(from_timestamp_ms)
            )
            result = {}
            for update in sorted(data or [], key=lambda x: x.get("time", 0)):
                delta = update.get("delta") or {}
                if delta.get("type") == "withdraw":
                    tx_hash = update.get("hash")
                    usdc_amount = float(delta.get("usdc", 0))
                    if tx_hash:
                        result[tx_hash] = usdc_amount

            return True, result

        except Exception as exc:
            self.logger.error(f"Failed to get user withdrawals: {exc}")
            return False, {}

    async def wait_for_withdrawal(
        self,
        address: str,
        *,
        lookback_s: int = 5,
        max_poll_time_s: int = 30 * 60,
        poll_interval_s: int = 5,
    ) -> tuple[bool, dict[str, float]]:
        start_time_ms = time.time() * 1000
        iterations = int(max_poll_time_s / poll_interval_s) + 1

        for i in range(iterations, 0, -1):
            check_from_ms = start_time_ms - (lookback_s * 1000)
            success, withdrawals = await self.get_user_withdrawals(
                address, int(check_from_ms)
            )

            if success and withdrawals:
                self.logger.info(
                    f"Found {len(withdrawals)} withdrawal(s): {withdrawals}"
                )
                return True, withdrawals

            remaining_s = i * poll_interval_s
            self.logger.info(
                f"Waiting for withdrawal to appear on-chain... "
                f"{remaining_s}s remaining (withdrawals often take a few minutes)"
            )
            await asyncio.sleep(poll_interval_s)

        self.logger.warning(
            f"No withdrawal detected after {max_poll_time_s}s. "
            "The withdrawal may still be processing."
        )
        return False, {}
