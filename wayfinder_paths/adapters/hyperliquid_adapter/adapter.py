from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from decimal import ROUND_DOWN, Decimal, getcontext
from typing import Any, Literal, cast

from aiocache import Cache
from eth_account.messages import encode_typed_data
from eth_utils import to_checksum_address
from hyperliquid.exchange import get_timestamp_ms
from hyperliquid.utils.signing import (
    BUILDER_FEE_SIGN_TYPES,
    SPOT_TRANSFER_SIGN_TYPES,
    USD_CLASS_TRANSFER_SIGN_TYPES,
    USER_DEX_ABSTRACTION_SIGN_TYPES,
    WITHDRAW_SIGN_TYPES,
    OrderType,
    OrderWire,
    float_to_wire,
    get_l1_action_payload,
    order_type_to_wire,
    order_wires_to_order_action,
    user_signed_payload,
)
from hyperliquid.utils.types import BuilderInfo
from loguru import logger

from wayfinder_paths.adapters.hyperliquid_adapter.info import get_info, get_perp_dexes
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

ARBITRUM_CHAIN_ID = "0xa4b1"
MAINNET = "Mainnet"
USER_DECLINED_ERROR = {
    "status": "err",
    "error": "User declined transaction. Please try again..",
}


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

        if sign_callback is not None:
            self._sign_callback: Callable[..., Awaitable[Any]] | None = sign_callback
            self._signing_type: Literal["eip712", "local"] = "eip712"
        elif config:
            self._sign_callback = create_local_signer(config)
            self._signing_type = "local"
        else:
            self._sign_callback = None
            self._signing_type = "local"

    async def _post_across_dexes(
        self,
        payload: dict[str, Any],
        aggregator: Callable[[list[Any]], Any],
        *,
        max_retries: int = 3,
    ) -> Any:
        """Post a payload to all perp dexes concurrently and aggregate results."""

        async def _post_one(dex: str) -> Any:
            body = {**payload, "dex": dex}
            last_exc: Exception | None = None
            for attempt in range(max_retries):
                try:
                    return get_info().post("/info", body)
                except Exception as exc:
                    last_exc = exc
                    if attempt < max_retries - 1:
                        await asyncio.sleep(0.5 * (2**attempt))
            self.logger.warning(
                f"All {max_retries} retries failed for dex={dex!r}: {last_exc}"
            )
            return None

        results = await asyncio.gather(*[_post_one(dex) for dex in get_perp_dexes()])
        return aggregator([r for r in results if r is not None])

    def _get_price_decimals(self, asset_id: int) -> int:
        is_spot = asset_id >= 10_000
        return (6 if not is_spot else 8) - self.get_sz_decimals(asset_id)

    def _sig_hex_to_hl_signature(self, sig_hex: str) -> dict[str, Any]:
        """Convert a 65-byte hex signature into Hyperliquid {r,s,v}."""
        if not isinstance(sig_hex, str) or not sig_hex.startswith("0x"):
            raise ValueError("Expected hex signature string starting with 0x")
        raw = bytes.fromhex(sig_hex[2:])
        if len(raw) != 65:
            raise ValueError(f"Expected 65-byte signature, got {len(raw)} bytes")

        r = raw[0:32]
        s = raw[32:64]
        v = raw[64]
        if v < 27:
            v += 27

        return {"r": f"0x{r.hex()}", "s": f"0x{s.hex()}", "v": int(v)}

    def _create_hypecore_order_actions(
        self,
        asset_id: int,
        is_buy: bool,
        price: float,
        size: float,
        reduce_only: bool,
        order_type: OrderType,
        builder: BuilderInfo | None = None,
        cloid: str | None = None,
    ) -> dict[str, Any]:
        order: OrderWire = {
            "a": asset_id,
            "b": is_buy,
            "p": float_to_wire(price),
            "s": float_to_wire(size),
            "r": reduce_only,
            "t": order_type_to_wire(order_type),
        }
        if cloid is not None:
            order["c"] = cloid
        return order_wires_to_order_action([order], builder)

    def _broadcast_hypecore(
        self, action: dict[str, Any], nonce: int, signature: dict[str, Any]
    ) -> dict[str, Any]:
        payload = {
            "action": action,
            "nonce": nonce,
            "signature": signature,
        }
        logger.info(f"Broadcasting Hypecore payload: {payload}")
        return get_info().post("/exchange", payload)

    async def _sign(
        self, payload: str, action: dict[str, Any], address: str
    ) -> dict[str, Any] | None:
        if self._sign_callback is None:
            raise ValueError(
                "Config required for local signing (no sign_callback provided)"
            )

        if self._signing_type == "eip712":
            sig_hex = await self._sign_callback(payload)
            if not sig_hex:
                return None
            return self._sig_hex_to_hl_signature(sig_hex)

        encoded_payload = encode_typed_data(full_message=payload)
        result = await self._sign_callback(action, encoded_payload, address)
        return cast(dict[str, Any] | None, result)

    async def _sign_and_broadcast_hypecore(
        self, action: dict[str, Any], address: str
    ) -> dict[str, Any]:
        nonce = get_timestamp_ms()
        payload = get_l1_action_payload(action, None, nonce, None, True)
        if not (sig := await self._sign(payload, action, address)):
            return USER_DECLINED_ERROR
        return self._broadcast_hypecore(action, nonce, sig)

    def _hypecore_get_user_transfers(
        self,
        user_address: str,
        from_timestamp_ms: int,
        transfer_type: Literal["deposit", "withdraw"],
    ) -> dict[str, Decimal]:
        data = get_info().post(
            "/info",
            {
                "type": "userNonFundingLedgerUpdates",
                "user": to_checksum_address(user_address),
                "startTime": int(from_timestamp_ms),
            },
        )
        res: dict[str, Decimal] = {}
        for u in sorted(data, key=lambda x: x.get("time", 0)):
            delta = u.get("delta")
            if delta and delta.get("type") == transfer_type:
                res[u["hash"]] = Decimal(str(delta["usdc"]))
        return res

    def hypecore_get_user_deposits(
        self, user_address: str, from_timestamp_ms: int
    ) -> dict[str, Decimal]:
        return self._hypecore_get_user_transfers(
            user_address=user_address,
            from_timestamp_ms=from_timestamp_ms,
            transfer_type="deposit",
        )

    def hypecore_get_user_withdrawals(
        self, user_address: str, from_timestamp_ms: int
    ) -> dict[str, Decimal]:
        return self._hypecore_get_user_transfers(
            user_address=user_address,
            from_timestamp_ms=from_timestamp_ms,
            transfer_type="withdraw",
        )

    async def get_meta_and_asset_ctxs(self) -> tuple[bool, Any]:
        cache_key = "hl_meta_and_asset_ctxs"
        cached = await self._cache.get(cache_key)
        if cached:
            return True, cached

        def _aggregate(results: list[list[Any]]) -> list[Any]:
            if not results:
                return [{}, []]
            merged_universe: list[dict[str, Any]] = []
            merged_ctxs: list[dict[str, Any]] = []
            for pair in results:
                meta_part = pair[0] if len(pair) > 0 else {}
                ctxs_part = pair[1] if len(pair) > 1 else []
                merged_universe.extend(meta_part.get("universe", []))
                merged_ctxs.extend(ctxs_part)
            return [{"universe": merged_universe}, merged_ctxs]

        try:
            data = await self._post_across_dexes(
                {"type": "metaAndAssetCtxs"}, _aggregate
            )
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
    ) -> tuple[Literal[True], dict[str, Any]] | tuple[Literal[False], str]:
        try:
            data = get_info().l2_snapshot(coin)
            return True, data
        except Exception as exc:
            self.logger.error(f"Failed to fetch L2 book for {coin}: {exc}")
            return False, str(exc)

    async def get_user_state(
        self, address: str
    ) -> tuple[Literal[True], dict[str, Any]] | tuple[Literal[False], str]:
        def _aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
            if not results:
                return {}
            base = results[0]
            for other in results[1:]:
                # Concatenate positions from all dexes
                base_positions = base.get("assetPositions", [])
                other_positions = other.get("assetPositions", [])
                base["assetPositions"] = base_positions + other_positions

                # Sum numeric margin fields
                for summary_key in ("marginSummary", "crossMarginSummary"):
                    base_summary = base.get(summary_key, {})
                    other_summary = other.get(summary_key, {})
                    for field in (
                        "accountValue",
                        "totalNtlPos",
                        "totalRawUsd",
                        "totalMarginUsed",
                    ):
                        base_val = float(base_summary.get(field, 0))
                        other_val = float(other_summary.get(field, 0))
                        base_summary[field] = str(base_val + other_val)
                    base[summary_key] = base_summary
            return base

        try:
            data = await self._post_across_dexes(
                {"type": "clearinghouseState", "user": address}, _aggregate
            )
            return True, data
        except Exception as exc:
            self.logger.error(f"Failed to fetch user_state for {address}: {exc}")
            return False, str(exc)

    async def get_spot_user_state(
        self, address: str
    ) -> tuple[Literal[True], dict[str, Any]] | tuple[Literal[False], str]:
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

        perp_result = await self.get_user_state(account)
        if perp_result[0]:
            ok_any = True
            out["perp"] = perp_result[1]
            out["positions"] = perp_result[1].get("assetPositions", [])
        else:
            out["errors"]["perp"] = perp_result[1]

        if include_spot:
            spot_result = await self.get_spot_user_state(account)
            if spot_result[0]:
                ok_any = True
                out["spot"] = spot_result[1]
            else:
                out["errors"]["spot"] = spot_result[1]

        if include_open_orders:
            if include_frontend_open_orders:
                orders_result = await self.get_frontend_open_orders(account)
            else:
                orders_result = await self.get_open_orders(account)
            if orders_result[0]:
                ok_any = True
                out["openOrders"] = orders_result[1]
            else:
                out["errors"]["openOrders"] = orders_result[1]

        return ok_any, out

    async def get_margin_table(
        self, margin_table_id: int
    ) -> tuple[Literal[True], list[dict]] | tuple[Literal[False], str]:
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

    async def get_spot_l2_book(
        self, spot_asset_id: int
    ) -> tuple[Literal[True], dict[str, Any]] | tuple[Literal[False], str]:
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
        def _aggregate(results: list[dict[str, str]]) -> dict[str, str]:
            merged: dict[str, str] = {}
            for mids in results:
                merged.update(mids)
            return merged

        try:
            data = await self._post_across_dexes({"type": "allMids"}, _aggregate)
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
        builder_fee = self._mandatory_builder_fee(builder)
        await self.ensure_dex_abstraction(address)
        await self.ensure_builder_fee_approved(address, builder_fee)

        asset_name = get_info().asset_to_coin[asset_id]
        ok, mids = await self.get_all_mid_prices()
        if not ok or asset_name not in mids:
            return False, {
                "status": "err",
                "error": f"Could not fetch mid price for {asset_name}",
            }
        midprice = mids[asset_name]

        if slippage >= 1 or slippage < 0:
            return False, {
                "status": "err",
                "error": f"slippage must be in [0, 1), got {slippage}",
            }

        price = midprice * ((1 + slippage) if is_buy else (1 - slippage))
        price = round(
            float(f"{price:.5g}"),
            self._get_price_decimals(asset_id),
        )
        order_actions = self._create_hypecore_order_actions(
            asset_id,
            is_buy,
            price,
            size,
            reduce_only,
            {"limit": {"tif": "Ioc"}},
            BuilderInfo(b=builder_fee.get("b"), f=builder_fee.get("f")),
            cloid,
        )
        result = await self._sign_and_broadcast_hypecore(order_actions, address)

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

        order_actions = {
            "type": "cancel",
            "cancels": [
                {
                    "a": asset_id,
                    "o": order_id_int,
                }
            ],
        }
        result = await self._sign_and_broadcast_hypecore(order_actions, address)

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
        nonce = get_timestamp_ms()
        action = {
            "type": "spotSend",
            "hyperliquidChain": MAINNET,
            "signatureChainId": hex(42161),  # Arbitrum
            "destination": str(destination),
            "token": str(token),
            "amount": str(amount),
            "time": nonce,
        }
        payload = user_signed_payload(
            "HyperliquidTransaction:SpotSend", SPOT_TRANSFER_SIGN_TYPES, action
        )
        if not (sig := await self._sign(payload, action, address)):
            return False, USER_DECLINED_ERROR
        result = self._broadcast_hypecore(action, nonce, sig)

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
        order_actions = {
            "type": "updateLeverage",
            "asset": asset_id,
            "isCross": is_cross,
            "leverage": leverage,
        }
        result = await self._sign_and_broadcast_hypecore(order_actions, address)

        success = result.get("status") == "ok"
        return success, result

    async def update_isolated_margin(
        self, asset_id: int, delta_usdc: float, address: str
    ) -> tuple[bool, dict[str, Any]]:
        """Add/remove USDC margin on an existing ISOLATED position.
        Works for both longs & shorts. Positive = add, negative = remove.
        """
        ntli = int(round(delta_usdc * 1_000_000))
        order_actions = {
            "type": "updateIsolatedMargin",
            "asset": asset_id,
            "isBuy": delta_usdc >= 0,
            "ntli": ntli,
        }
        result = await self._sign_and_broadcast_hypecore(order_actions, address)

        success = result.get("status") == "ok"
        return success, result

    async def _usd_class_transfer(
        self, amount: float, address: str, to_perp: bool
    ) -> tuple[bool, dict[str, Any]]:
        nonce = get_timestamp_ms()
        action = {
            "hyperliquidChain": MAINNET,
            "signatureChainId": ARBITRUM_CHAIN_ID,
            "amount": str(amount),
            "toPerp": to_perp,
            "nonce": nonce,
            "type": "usdClassTransfer",
        }
        payload = user_signed_payload(
            "HyperliquidTransaction:UsdClassTransfer",
            USD_CLASS_TRANSFER_SIGN_TYPES,
            action,
        )
        if not (sig := await self._sign(payload, action, address)):
            return False, USER_DECLINED_ERROR
        result = self._broadcast_hypecore(action, nonce, sig)

        success = result.get("status") == "ok"
        return success, result

    async def transfer_spot_to_perp(
        self,
        amount: float,
        address: str,
    ) -> tuple[bool, dict[str, Any]]:
        return await self._usd_class_transfer(
            amount=amount, address=address, to_perp=True
        )

    async def transfer_perp_to_spot(
        self,
        amount: float,
        address: str,
    ) -> tuple[bool, dict[str, Any]]:
        return await self._usd_class_transfer(
            amount=amount, address=address, to_perp=False
        )

    async def place_trigger_order(
        self,
        asset_id: int,
        is_buy: bool,
        trigger_price: float,
        size: float,
        address: str,
        tpsl: Literal["tp", "sl"],
        is_market: bool = True,
        limit_price: float | None = None,
        builder: dict[str, Any] | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        builder_fee = self._mandatory_builder_fee(builder)
        await self.ensure_dex_abstraction(address)
        await self.ensure_builder_fee_approved(address, builder_fee)
        builder_info = BuilderInfo(b=builder_fee.get("b"), f=builder_fee.get("f"))

        order_type: OrderType = {
            "trigger": {"triggerPx": trigger_price, "isMarket": is_market, "tpsl": tpsl}
        }
        price = trigger_price if is_market else (limit_price or trigger_price)
        order_actions = self._create_hypecore_order_actions(
            asset_id, is_buy, price, size, True, order_type, builder_info
        )
        result = await self._sign_and_broadcast_hypecore(order_actions, address)

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
        return await self.place_trigger_order(
            asset_id=asset_id,
            is_buy=is_buy,
            trigger_price=trigger_price,
            size=size,
            address=address,
            tpsl="sl",
            is_market=True,
        )

    async def get_user_fills(
        self, address: str
    ) -> tuple[bool, list[dict[str, Any]]] | tuple[Literal[False], str]:
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
    ) -> tuple[Literal[True], dict[str, Any]] | tuple[Literal[False], str]:
        try:
            data = get_info().query_order_by_oid(address, int(order_id))
            return True, data
        except Exception as exc:
            self.logger.error(f"Failed to fetch order_status for {order_id}: {exc}")
            return False, str(exc)

    async def get_open_orders(
        self, address: str
    ) -> tuple[Literal[True], list[dict[str, Any]]] | tuple[Literal[False], str]:
        def _aggregate(results: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
            merged: list[dict[str, Any]] = []
            for orders in results:
                if isinstance(orders, list):
                    merged.extend(orders)
            return merged

        try:
            data = await self._post_across_dexes(
                {"type": "openOrders", "user": address}, _aggregate
            )
            return True, data
        except Exception as exc:
            self.logger.error(f"Failed to fetch open_orders for {address}: {exc}")
            return False, str(exc)

    async def get_frontend_open_orders(
        self, address: str
    ) -> tuple[bool, list[dict[str, Any]]]:
        def _aggregate(results: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
            merged: list[dict[str, Any]] = []
            for orders in results:
                if isinstance(orders, list):
                    merged.extend(orders)
            return merged

        try:
            data = await self._post_across_dexes(
                {"type": "frontendOpenOrders", "user": address}, _aggregate
            )
            return True, data
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
        nonce = get_timestamp_ms()
        action = {
            "hyperliquidChain": MAINNET,
            "signatureChainId": ARBITRUM_CHAIN_ID,
            "destination": address,
            "amount": str(amount),
            "time": nonce,
            "type": "withdraw3",
        }
        payload = user_signed_payload(
            "HyperliquidTransaction:Withdraw", WITHDRAW_SIGN_TYPES, action
        )
        if not (sig := await self._sign(payload, action, address)):
            return False, USER_DECLINED_ERROR
        result = self._broadcast_hypecore(action, nonce, sig)
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
        nonce = get_timestamp_ms()
        action = {
            "hyperliquidChain": MAINNET,
            "signatureChainId": ARBITRUM_CHAIN_ID,
            "maxFeeRate": max_fee_rate,
            "builder": builder,
            "nonce": nonce,
            "type": "approveBuilderFee",
        }
        payload = user_signed_payload(
            "HyperliquidTransaction:ApproveBuilderFee", BUILDER_FEE_SIGN_TYPES, action
        )
        if not (sig := await self._sign(payload, action, address)):
            return False, USER_DECLINED_ERROR
        result = self._broadcast_hypecore(action, nonce, sig)

        success = result.get("status") == "ok"
        return success, result

    async def set_dex_abstraction(
        self, address: str, enabled: bool
    ) -> tuple[bool, dict[str, Any]]:
        nonce = get_timestamp_ms()
        action = {
            "hyperliquidChain": MAINNET,
            "signatureChainId": ARBITRUM_CHAIN_ID,
            "user": address.lower(),
            "enabled": enabled,
            "nonce": nonce,
            "type": "userDexAbstraction",
        }
        payload = user_signed_payload(
            "HyperliquidTransaction:UserDexAbstraction",
            USER_DEX_ABSTRACTION_SIGN_TYPES,
            action,
        )
        if not (sig := await self._sign(payload, action, address)):
            return False, USER_DECLINED_ERROR
        result = self._broadcast_hypecore(action, nonce, sig)

        success = result.get("status") == "ok"
        return success, result

    async def ensure_dex_abstraction(self, address: str) -> tuple[bool, str]:
        """Enable dex abstraction if not already enabled (required for HIP-3 dex trading)."""
        try:
            state = get_info().query_user_dex_abstraction_state(address)
            if state:
                return True, "Dex abstraction already enabled"
        except Exception as exc:
            logger.warning(
                f"Failed to query dex abstraction state: {exc}, proceeding with enable"
            )

        ok, result = await self.set_dex_abstraction(address, enabled=True)
        if ok:
            return True, "Dex abstraction enabled"
        return False, f"Failed to enable dex abstraction: {result}"

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
        cloid: str | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        builder_fee = self._mandatory_builder_fee(builder)
        await self.ensure_dex_abstraction(address)
        await self.ensure_builder_fee_approved(address, builder_fee)
        order_actions = self._create_hypecore_order_actions(
            asset_id,
            is_buy,
            price,
            size,
            reduce_only,
            {"limit": {"tif": "Gtc"}},
            BuilderInfo(b=builder_fee.get("b"), f=builder_fee.get("f")),
            cloid,
        )
        result = await self._sign_and_broadcast_hypecore(order_actions, address)

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
