from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from decimal import ROUND_DOWN, Decimal, getcontext
from typing import Any

from aiocache import Cache
from eth_utils import to_checksum_address
from hyperliquid.info import Info
from hyperliquid.utils import constants
from loguru import logger

from wayfinder_paths.adapters.hyperliquid_adapter.exchange import Exchange
from wayfinder_paths.adapters.hyperliquid_adapter.local_signer import (
    create_local_signer,
)
from wayfinder_paths.adapters.hyperliquid_adapter.util import Util
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
        self._info: Any | None = None
        self._util: Any | None = None

        self._sign_callback = sign_callback
        self._exchange: Exchange | None = None

        self._asset_to_sz_decimals: dict[int, int] | None = None
        self._coin_to_asset: dict[str, int] | None = None

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
                info=self.info,
                util=self.util,
                sign_callback=sign_callback,
                signing_type=signing_type,
            )
        return self._exchange

    @property
    def info(self) -> Any:
        # Lazily initialize the Hyperliquid SDK so read-only paths can work
        # without full signing configuration.
        if self._info is None:
            self._info = Info(constants.MAINNET_API_URL, skip_ws=True)
        return self._info

    @info.setter
    def info(self, value: Any) -> None:
        self._info = value
        self._util = None
        self._asset_to_sz_decimals = None
        self._coin_to_asset = None

    @property
    def util(self) -> Any:
        if self._util is None:
            self._util = Util(self.info)
        return self._util

    @util.setter
    def util(self, value: Any) -> None:
        self._util = value

    # ------------------------------------------------------------------ #
    # Market Data - Read Operations                                       #
    # ------------------------------------------------------------------ #

    async def get_meta_and_asset_ctxs(self) -> tuple[bool, Any]:
        cache_key = "hl_meta_and_asset_ctxs"
        cached = await self._cache.get(cache_key)
        if cached:
            return True, cached

        try:
            data = self.info.meta_and_asset_ctxs()
            # Cache for 1 minute
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
            spot_meta = self.info.spot_meta
            if callable(spot_meta):
                data = spot_meta()
            else:
                data = spot_meta
            await self._cache.set(cache_key, data, ttl=60)
            return True, data
        except Exception as exc:
            self.logger.error(f"Failed to fetch spot_meta: {exc}")
            return False, str(exc)

    async def get_spot_token_sz_decimals(self, coin: str) -> int | None:
        try:
            success, spot_meta = await self.get_spot_meta()
            if not success or not isinstance(spot_meta, dict):
                return None

            for token in spot_meta.get("tokens", []):
                name = token.get("name") or token.get("coin") or token.get("symbol")
                if not name:
                    continue
                if str(name).upper() != str(coin).upper():
                    continue
                sz_decimals = token.get("szDecimals") or token.get("sz_decimals")
                if sz_decimals is None:
                    return None
                return int(sz_decimals)
        except Exception:  # noqa: BLE001
            return None
        return None

    @staticmethod
    def max_transferable_amount(
        total: str,
        hold: str,
        *,
        sz_decimals: int,
        leave_one_tick: bool = True,
    ) -> float:
        """Compute a safe transferable amount (Decimal math, round down, leave 1 tick).

        Hyperliquid requires amounts to respect szDecimals. This helper avoids
        float rounding edge cases by:
        - parsing balances as Decimal
        - computing available = total - hold
        - rounding down to sz_decimals
        - optionally leaving 1 tick so we don't request the full balance
        """
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

            # Cache for 5 min
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
            data = self.info.l2_snapshot(coin)
            return True, data
        except Exception as exc:
            self.logger.error(f"Failed to fetch L2 book for {coin}: {exc}")
            return False, str(exc)

    async def get_user_state(self, address: str) -> tuple[bool, dict[str, Any]]:
        try:
            data = self.info.user_state(address)
            return True, data
        except Exception as exc:
            self.logger.error(f"Failed to fetch user_state for {address}: {exc}")
            return False, str(exc)

    async def get_spot_user_state(self, address: str) -> tuple[bool, dict[str, Any]]:
        try:
            data = self.info.spot_user_state(address)
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
        """
        Full Hyperliquid user state snapshot.

        Includes perp positions (user_state), optional spot balances, and optional open
        orders (frontendOpenOrders by default, since it includes trigger orders).
        """
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
            # Hyperliquid expects `id` for margin tables in the /info API.
            # Keep a fallback to `marginTableId` for compatibility with older SDKs.
            body = {"type": "marginTable", "id": int(margin_table_id)}
            try:
                data = self.info.post("/info", body)
            except Exception:  # noqa: BLE001 - try alternate payload key
                body = {"type": "marginTable", "marginTableId": int(margin_table_id)}
                data = self.info.post("/info", body)
            await self._cache.set(cache_key, data, ttl=86400)  # Cache for 24h
            return True, data
        except Exception as exc:
            self.logger.error(f"Failed to fetch margin_table {margin_table_id}: {exc}")
            return False, str(exc)

    async def get_spot_l2_book(self, spot_asset_id: int) -> tuple[bool, dict[str, Any]]:
        try:
            # Spot L2 uses different coin names based on spot index:
            # - Index 0 (PURR): use "PURR/USDC"
            # - All other indices: use "@{index}"
            spot_index = (
                spot_asset_id - 10000 if spot_asset_id >= 10000 else spot_asset_id
            )

            if spot_index == 0:
                coin = "PURR/USDC"
            else:
                coin = f"@{spot_index}"

            body = {"type": "l2Book", "coin": coin}
            data = self.info.post("/info", body)
            return True, data
        except Exception as exc:
            self.logger.error(
                f"Failed to fetch spot L2 book for {spot_asset_id}: {exc}"
            )
            return False, str(exc)

    # ------------------------------------------------------------------ #
    # Asset Mappings                                                      #
    # ------------------------------------------------------------------ #

    @property
    def asset_to_sz_decimals(self) -> dict[int, int]:
        if self._asset_to_sz_decimals is None:
            self._asset_to_sz_decimals = dict(self.info.asset_to_sz_decimals)
        return self._asset_to_sz_decimals

    @property
    def coin_to_asset(self) -> dict[str, int]:
        """Get coin name to asset ID mapping (perps only)."""
        if self._coin_to_asset is None:
            self._coin_to_asset = dict(self.info.coin_to_asset)
        return self._coin_to_asset

    def get_sz_decimals(self, asset_id: int) -> int:
        try:
            return self.asset_to_sz_decimals[asset_id]
        except KeyError:
            raise ValueError(
                f"Unknown asset_id {asset_id}: missing szDecimals"
            ) from None

    async def refresh_mappings(self) -> None:
        self._asset_to_sz_decimals = None
        self._coin_to_asset = None
        await self._cache.clear()

    async def get_asset_id(self, coin: str, is_perp: bool) -> int | None:
        if is_perp:
            return (self.coin_to_asset or {}).get(coin.upper())
        else:
            ok, assets = await self.get_spot_assets()
            if not ok:
                return None
            return assets.get(f"{coin.upper()}/USDC")

    # ------------------------------------------------------------------ #
    # Utility Methods                                                      #
    # ------------------------------------------------------------------ #

    async def get_all_mid_prices(self) -> tuple[bool, dict[str, float]]:
        try:
            data = self.info.all_mids()
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

    # ------------------------------------------------------------------ #
    # Execution Methods (require signing callback)                         #
    # ------------------------------------------------------------------ #

    def _mandatory_builder_fee(self, builder: dict[str, Any] | None) -> dict[str, Any]:
        """
        Resolve the builder fee config to attach to orders.

        Builder attribution is mandatory in this repo and is always directed to
        the Wayfinder builder wallet (`HYPE_FEE_WALLET`).
        """
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
        """
        Place a market order (IOC with slippage).

        Args:
            asset_id: Asset ID (perp < 10000, spot >= 10000)
            is_buy: True for buy, False for sell
            slippage: Slippage tolerance (0.0 to 1.0)
            size: Order size in base units
            address: Wallet address
            reduce_only: If True, only reduce existing position
            cloid: Client order ID (optional)
            builder: Builder fee config; if omitted, a mandatory default is applied.

        Returns:
            (success, response_data or error_message)
        """
        builder = self._mandatory_builder_fee(builder)

        from hyperliquid.utils.types import BuilderInfo

        builder_info = BuilderInfo(b=builder.get("b"), f=builder.get("f"))
        result = await self.exchange.place_market_order(
            asset_id=asset_id,
            is_buy=is_buy,
            slippage=slippage,
            size=size,
            address=address,
            reduce_only=reduce_only,
            cloid=cloid,
            builder=builder_info,
        )

        # Check both the API status and the order statuses for errors
        success = result.get("status") == "ok"
        if success:
            # Check if the order itself has errors in statuses
            response = result.get("response", {})
            data = response.get("data", {})
            statuses = data.get("statuses", [])
            for status in statuses:
                if isinstance(status, dict) and status.get("error"):
                    success = False
                    break
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
        """
        Cancel order by client order ID (CLOID).

        Note: The Exchange class uses cancel_order with order_id.
        For cloid-based cancellation, we'd need to first fetch the order_id from open orders.
        """
        # Find order_id from open orders
        success, orders = await self.get_frontend_open_orders(address)
        if not success:
            return False, {
                "status": "err",
                "response": {"type": "error", "data": "Could not fetch open orders"},
            }

        # Find matching order by cloid
        matching_order = None
        for order in orders:
            if order.get("cloid") == cloid:
                matching_order = order
                break

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
        """
        Transfer a spot token to a destination address (signed spotSend action).

        This is used for:
        - user-to-user spot transfers
        - HyperCore → HyperEVM routing (destination = system address)
        """
        # Arbitrum chain ID
        result = await self.exchange.spot_transfer(
            signature_chain_id=42161,
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
        """
        Resolve spot token metadata from Hyperliquid spot meta by EVM contract address.

        Special-case: native HYPE uses the 0-address and maps to tokens[150].
        """
        token_addr = (token_address or ZERO_ADDRESS).strip()
        token_addr_lower = token_addr.lower()

        success, spot_meta = await self.get_spot_meta()
        if not success or not isinstance(spot_meta, dict):
            return None

        tokens = spot_meta.get("tokens", [])
        if not isinstance(tokens, list) or not tokens:
            return None

        if token_addr_lower == ZERO_ADDRESS.lower():
            token = tokens[150] if len(tokens) > 150 else None
            return token if isinstance(token, dict) else None

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
        """
        Transfer a spot token from HyperCore (Hyperliquid spot) to HyperEVM.

        Notes:
        - destination is the token's HyperEVM system address (NOT the user's wallet)
        - token is formatted as "name:tokenId" from spot meta
        """
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
            data = self.info.user_fills(address)
            return True, data if isinstance(data, list) else []
        except Exception as exc:
            self.logger.error(f"Failed to fetch user_fills for {address}: {exc}")
            return False, str(exc)

    async def check_recent_liquidations(
        self, address: str, since_ms: int
    ) -> tuple[bool, list[dict[str, Any]]]:
        """
        Check if user was liquidated since a given timestamp.

        Fills have an optional 'liquidation' field with:
        - liquidatedUser: who got liquidated
        - markPx: price at liquidation
        - method: "market" or "backstop"

        Args:
            address: Wallet address
            since_ms: Epoch milliseconds to check from

        Returns:
            (success, list of liquidation fills where user was liquidated)
        """
        try:
            now_ms = int(time.time() * 1000)
            body = {
                "type": "userFillsByTime",
                "user": address,
                "startTime": since_ms,
                "endTime": now_ms,
            }
            data = self.info.post("/info", body)
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
            body = {"type": "orderStatus", "user": address, "oid": order_id}
            data = self.info.post("/info", body)
            return True, data
        except Exception as exc:
            self.logger.error(f"Failed to fetch order_status for {order_id}: {exc}")
            return False, str(exc)

    async def get_open_orders(self, address: str) -> tuple[bool, list[dict[str, Any]]]:
        try:
            data = self.info.open_orders(address)
            return True, data if isinstance(data, list) else []
        except Exception as exc:
            self.logger.error(f"Failed to fetch open_orders for {address}: {exc}")
            return False, str(exc)

    async def get_frontend_open_orders(
        self, address: str
    ) -> tuple[bool, list[dict[str, Any]]]:
        """
        Get all open orders including trigger orders (stop-loss, take-profit).

        Uses frontendOpenOrders endpoint which returns both limit and trigger orders
        with full order details including orderType and triggerPx.

        Args:
            address: Wallet address

        Returns:
            List of open order records including trigger orders
        """
        try:
            data = self.info.frontend_open_orders(address)
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
        """
        Withdraw USDC from Hyperliquid to Arbitrum.

        Note: This is an L1 withdrawal handled by the Hyperliquid executor (signing required).
        """
        result = await self.exchange.withdraw(
            amount=amount,
            address=address,
        )
        success = result.get("status") == "ok"
        return success, result

    # ------------------------------------------------------------------ #
    # Deposit/Withdrawal Helpers                                          #
    # ------------------------------------------------------------------ #

    def get_perp_margin_amount(self, user_state: dict[str, Any]) -> float:
        try:
            margin_summary = user_state.get("marginSummary", {})
            account_value = margin_summary.get("accountValue")
            if account_value is not None:
                return float(account_value)
            # Fallback to crossMarginSummary
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
            data = self.info.post("/info", body)
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
        """
        Place a limit order (GTC - Good Till Cancelled).

        Used for spot stop-loss orders in basis trading.

        Args:
            asset_id: Asset ID (perp < 10000, spot >= 10000)
            is_buy: True for buy, False for sell
            price: Limit price
            size: Order size
            address: Wallet address
            reduce_only: If True, only reduces existing position
            builder: Builder fee config; if omitted, a mandatory default is applied.

        Returns:
            (success, response_data or error_message)
        """
        builder = self._mandatory_builder_fee(builder)

        # Convert builder to BuilderInfo
        from hyperliquid.utils.types import BuilderInfo

        builder_info = BuilderInfo(b=builder.get("b"), f=builder.get("f"))

        result = await self.exchange.place_limit_order(
            asset_id=asset_id,
            is_buy=is_buy,
            price=price,
            size=size,
            address=address,
            builder=builder_info,
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
        iterations = timeout_s // poll_interval_s

        # Get initial balance
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

        for i in range(iterations):
            await asyncio.sleep(poll_interval_s)

            success, state = await self.get_user_state(address)
            if not success:
                continue

            current_balance = self.get_perp_margin_amount(state)

            # Allow 5% tolerance for fees/slippage
            if current_balance >= initial_balance + expected_increase * 0.95:
                self.logger.info(
                    f"Hyperliquid deposit confirmed: ${current_balance - initial_balance:.2f} "
                    f"(expected ${expected_increase:.2f})"
                )
                return True, current_balance

            remaining_s = (iterations - i - 1) * poll_interval_s
            self.logger.debug(
                f"Waiting for deposit... current=${current_balance:.2f}, "
                f"need=${initial_balance + expected_increase:.2f}, {remaining_s}s remaining"
            )

        self.logger.warning(
            f"Hyperliquid deposit not confirmed after {timeout_s}s. "
            "Deposits typically credit in < 1 minute (but can take longer)."
        )
        # Return current balance even if not confirmed
        success, state = await self.get_user_state(address)
        final_balance = (
            self.get_perp_margin_amount(state) if success else initial_balance
        )
        return False, final_balance

    async def get_user_withdrawals(
        self,
        address: str,
        from_timestamp_ms: int,
    ) -> tuple[bool, dict[str, float]]:
        try:
            from eth_utils import to_checksum_address

            data = self.info.post(
                "/info",
                {
                    "type": "userNonFundingLedgerUpdates",
                    "user": to_checksum_address(address),
                    "startTime": int(from_timestamp_ms),
                },
            )

            result = {}
            # Sort earliest to latest
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
        """
        Wait for a withdrawal to appear on-chain.

        Polls Hyperliquid's ledger updates until a withdrawal is detected.
        Withdrawals typically take ~3-4 minutes to process (but can take longer).

        Args:
            address: Wallet address
            lookback_s: How far back to look for withdrawals (small buffer for latency)
            max_poll_time_s: Maximum time to wait (default 30 minutes)
            poll_interval_s: Time between polls

        Returns:
            (success, {tx_hash: usdc_amount}) - withdrawals found
        """
        import time

        start_time_ms = time.time() * 1000
        iterations = int(max_poll_time_s / poll_interval_s) + 1

        for i in range(iterations, 0, -1):
            # Check for withdrawals since just before we started
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
