from __future__ import annotations

import asyncio
import binascii
import os
import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any, Literal

from loguru import logger

from wayfinder_paths.adapters.hyperliquid_adapter.adapter import HyperliquidAdapter

MIN_NOTIONAL_USD = 10.0


def _now_ms() -> int:
    return int(time.time() * 1000)


def _rand_cloid(prefix: str = "0x") -> str:
    return prefix + binascii.hexlify(os.urandom(16)).decode()


def _round_down_units(units: float, step: Decimal) -> float:
    if units <= 0:
        return 0.0
    if step == 0:
        return units
    quantized = Decimal(str(units)) / step
    return float(quantized.to_integral_value(rounding=ROUND_DOWN) * step)


def _round_up_units(units: float, step: Decimal) -> float:
    if units <= 0:
        return 0.0
    if step == 0:
        return units
    quantized = Decimal(str(units)) / step
    return float(quantized.to_integral_value(rounding=ROUND_UP) * step)


def _parse_oids_and_immediate_fill(
    resp: dict[str, Any],
) -> tuple[list[int], float, float]:
    oids: list[int] = []
    filled_units = 0.0
    filled_notional = 0.0

    if resp.get("status") != "ok":
        return oids, filled_units, filled_notional

    data = (resp.get("response") or {}).get("data") or {}
    for status in data.get("statuses", []):
        filled = status.get("filled") or {}
        resting = status.get("resting") or {}

        for section in (filled, resting):
            oid_val = section.get("oid")
            if oid_val is None:
                continue
            try:
                oid_int = int(oid_val)
            except (TypeError, ValueError):
                continue
            if oid_int not in oids:
                oids.append(oid_int)

        size = None
        for key in ("totalSz", "sz", "size", "quantity"):
            val = filled.get(key)
            try:
                size = float(val)
                break
            except (TypeError, ValueError, AttributeError):
                continue

        if size is None or size == 0:
            continue

        price = None
        for key in ("avgPx", "px", "price"):
            val = filled.get(key)
            try:
                price = float(val)
                break
            except (TypeError, ValueError, AttributeError):
                continue

        filled_units += abs(size)
        if price is not None:
            filled_notional += abs(size) * price
        else:
            notional = None
            for key in ("usdValue", "notional", "value"):
                val = filled.get(key)
                try:
                    notional = float(val)
                    break
                except (TypeError, ValueError, AttributeError):
                    continue
            if notional is not None:
                filled_notional += abs(notional)

    return oids, filled_units, filled_notional


@dataclass
class FillConfig:
    max_slip_bps: int = 35
    max_chunk_usd: float = 7_500.0
    max_loops: int = 40
    residual_shrink: float = 0.90


@dataclass
class FillConfirmCfg:
    max_status_polls: int = 4
    poll_sleep_s: float = 0.20
    fills_time_early_ms: int = 3_000
    fills_time_late_ms: int = 8_000


@dataclass
class LegFillResult:
    units: float = 0.0
    notional: float = 0.0


@dataclass
class LegSubmitResult:
    oids: list[int]
    start_ms: int
    coin_label: str
    immediate_units: float
    immediate_notional: float
    response: dict[str, Any]


class LegConfirmer:
    def __init__(self, adapter: HyperliquidAdapter, cfg: FillConfirmCfg):
        self.adapter = adapter
        self.cfg = cfg

    async def confirm_leg(
        self,
        *,
        address: str,
        coin_label: str,
        initial_oids: list[int],
        cloid: str | None,
        start_ms: int,
        fallback_units: float = 0.0,
        fallback_notional: float = 0.0,
    ) -> LegFillResult:
        oids: list[int] = list(initial_oids)

        if not oids and cloid:
            oid = await self._oid_from_cloid(cloid, address)
            if oid is not None:
                oids.append(oid)

        for _ in range(self.cfg.max_status_polls):
            await self._ensure_not_open(address, oids)
            if oids:
                numeric_or_cloid = oids[0]
                try:
                    numeric_or_cloid = int(numeric_or_cloid)
                except (TypeError, ValueError):
                    pass
                try:
                    success, status = await self.adapter.get_order_status(
                        address, numeric_or_cloid
                    )
                    if success and status.get("status") in {
                        "filled",
                        "canceled",
                        "triggered",
                    }:
                        break
                except Exception as exc:
                    logger.info(f"orderStatus poll failed for oid {oids[0]}: {exc}")
            await asyncio.sleep(self.cfg.poll_sleep_s)

        result = await self._sum_fills_by_oid_window(address, start_ms, oids)
        if result.units <= 0.0 and fallback_units > 0.0:
            logger.info(
                "Using fallback immediate fill for {}; confirmed units were zero.",
                coin_label,
            )
            return LegFillResult(units=fallback_units, notional=fallback_notional)
        return result

    async def _oid_from_cloid(self, cloid: str, address: str) -> int | None:
        try:
            success, status = await self.adapter.get_order_status(address, cloid)
            if not success:
                return None
        except Exception as exc:
            logger.warning(f"orderStatus by cloid failed for {cloid}: {exc}")
            return None

        order = status.get("order")
        if not isinstance(order, dict):
            return None
        oid = order.get("oid")
        try:
            return int(oid) if oid is not None else None
        except (TypeError, ValueError):
            return None

    async def _ensure_not_open(self, address: str, oids: list[int]) -> None:
        if not oids:
            return
        try:
            success, state = await self.adapter.get_user_state(address)
            if not success:
                return
        except Exception as exc:
            logger.info(
                f"Failed to fetch user state while ensuring orders closed: {exc}"
            )
            return

        if not isinstance(state, dict):
            return

        open_orders = state.get("openOrders", [])
        flattened = self._flatten_open_orders(open_orders)
        target = {int(o) for o in oids if isinstance(o, int)}
        attempted: set[int] = set()

        for entry in flattened:
            oid_val = entry.get("oid")
            try:
                oid_int = int(oid_val)
            except (TypeError, ValueError):
                continue
            if oid_int not in target or oid_int in attempted:
                continue
            asset_id = self._resolve_asset_id(entry)
            if asset_id is None:
                continue
            try:
                await self.adapter.cancel_order(asset_id, oid_int, address)
                attempted.add(oid_int)
            except Exception as exc:
                logger.info(f"Cancel failed for oid {oid_int}: {exc}")

    def _resolve_asset_id(self, order: dict[str, Any]) -> int | None:
        coin = order.get("coin")
        if not isinstance(coin, str):
            return None
        is_perp = "/" not in coin
        try:
            if is_perp:
                return self.adapter.coin_to_asset.get(coin)
            # For spot, would need spot meta lookup
            return None
        except Exception as exc:
            logger.info(f"Failed to resolve asset id for {coin}: {exc}")
            return None

    def _flatten_open_orders(self, obj: Any) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []

        def walk(node: Any, coin_ctx: str | None = None) -> None:
            if isinstance(node, dict):
                coin_ctx = node.get("coin", coin_ctx)
                oid = node.get("oid")
                if oid is not None:
                    enriched = dict(node)
                    if "coin" not in enriched and coin_ctx:
                        enriched["coin"] = coin_ctx
                    results.append(enriched)
                resting = node.get("resting")
                if isinstance(resting, dict):
                    enriched = dict(resting)
                    if "coin" not in enriched and coin_ctx:
                        enriched["coin"] = coin_ctx
                    results.append(enriched)
                for value in node.values():
                    if isinstance(value, (dict, list)):
                        walk(value, coin_ctx)
            elif isinstance(node, list):
                for item in node:
                    walk(item, coin_ctx)

        walk(obj)
        return results

    async def _sum_fills_by_oid_window(
        self,
        address: str,
        start_ms: int,
        oids: list[int],
    ) -> LegFillResult:
        if not oids:
            return LegFillResult()

        try:
            success, raw_fills = await self.adapter.get_user_fills(address)
            if not success:
                return LegFillResult()
        except Exception as exc:
            logger.warning(f"Failed to fetch user fills: {exc}")
            return LegFillResult()

        records = self._to_records(raw_fills)
        if not records:
            return LegFillResult()

        early = self.cfg.fills_time_early_ms + 2000
        late = self.cfg.fills_time_late_ms + 6000
        t0 = start_ms - early
        t1 = start_ms + late
        oid_strs = {str(o) for o in oids if o is not None}

        total_units = 0.0
        total_notional = 0.0

        for row in records:
            time_val = row.get("time")
            try:
                time_int = int(time_val)
            except (TypeError, ValueError):
                time_int = None

            if time_int is not None and (time_int < t0 or time_int > t1):
                continue

            oid_val = row.get("oid")
            if oid_val is None or str(oid_val) not in oid_strs:
                continue

            size = None
            for key in ("sz", "size", "quantity", "totalSz"):
                val = row.get(key)
                try:
                    size = float(val)
                    break
                except (TypeError, ValueError):
                    continue
            if size is None:
                continue

            price = None
            for key in ("px", "price", "avgPx"):
                val = row.get(key)
                try:
                    price = float(val)
                    break
                except (TypeError, ValueError):
                    continue

            total_units += abs(size)
            if price is not None:
                total_notional += abs(size) * price
            else:
                notional = None
                for key in ("usdValue", "notional", "value"):
                    val = row.get(key)
                    try:
                        notional = float(val)
                        break
                    except (TypeError, ValueError):
                        continue
                if notional is not None:
                    total_notional += abs(notional)

        return LegFillResult(units=total_units, notional=total_notional)

    @staticmethod
    def _to_records(data: Any) -> list[dict[str, Any]]:
        if data is None:
            return []
        if hasattr(data, "to_dict"):
            try:
                records = data.to_dict("records")
            except Exception:
                records = []
            return [r for r in records if isinstance(r, dict)]
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
        return []


class PairedFiller:
    def __init__(
        self,
        adapter: HyperliquidAdapter,
        address: str,
        cfg: FillConfig | None = None,
        confirm_cfg: FillConfirmCfg | None = None,
    ):
        self.adapter = adapter
        self.address = address
        self.cfg = cfg or FillConfig()
        self.confirm_cfg = confirm_cfg or FillConfirmCfg()
        self.confirmer = LegConfirmer(adapter, self.confirm_cfg)

    async def fill_pair_units(
        self,
        coin: str,
        spot_asset_id: int,
        perp_asset_id: int,
        total_units: float,
        direction: Literal[
            "long_spot_short_perp", "short_spot_long_perp"
        ] = "long_spot_short_perp",
        builder_fee: dict[str, Any] | None = None,
    ) -> tuple[
        float,
        float,
        float,
        float,
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        step = self._common_step(spot_asset_id, perp_asset_id)
        remaining = _round_down_units(total_units, step)
        if remaining <= 0:
            return 0.0, 0.0, 0.0, 0.0, [], []

        spot_is_buy = direction == "long_spot_short_perp"
        perp_is_buy = direction == "short_spot_long_perp"
        slip_bps = self.cfg.max_slip_bps
        slip_fraction = slip_bps / 10_000

        success, mids = await self.adapter.get_all_mid_prices()
        if not success:
            raise ValueError("Cannot fetch mid prices")
        mid_price = float(mids.get(coin, 0.0))
        if mid_price <= 0:
            raise ValueError(f"Cannot determine mid price for {coin}")

        max_chunk_units = _round_down_units(self.cfg.max_chunk_usd / mid_price, step)
        if max_chunk_units <= 0:
            max_chunk_units = float(step)
        min_units = self._min_units_for_notional(mid_price, step)
        if max_chunk_units < min_units:
            max_chunk_units = min_units

        loops = 0
        delta_units = 0.0
        total_spot = 0.0
        total_perp = 0.0
        total_spot_notional = 0.0
        total_perp_notional = 0.0
        spot_pointers: list = []
        perp_pointers: list = []
        step_float = float(step)

        while loops < self.cfg.max_loops and (
            remaining > 0 or abs(delta_units) >= step_float
        ):
            if abs(delta_units) >= step_float:
                loops += 1
                fix_units = _round_down_units(abs(delta_units), step)
                if fix_units <= 0:
                    delta_units = 0.0
                    continue
                if fix_units < min_units:
                    logger.warning(
                        f"Remaining imbalance {delta_units:.4f} {coin} below venue's minimum; leaving residual."
                    )
                    break

                if delta_units > 0:
                    # More spot than perp - need more perp
                    cl_fix = _rand_cloid()
                    perp_submit = await self._ioc_leg(
                        is_spot=False,
                        asset_id=perp_asset_id,
                        coin=coin,
                        side_is_buy=perp_is_buy,
                        units=fix_units,
                        slip_fraction=slip_fraction,
                        cloid=cl_fix,
                        builder_fee=builder_fee,
                    )
                    perp_fix = await self.confirmer.confirm_leg(
                        address=self.address,
                        coin_label=perp_submit.coin_label,
                        initial_oids=perp_submit.oids,
                        cloid=cl_fix,
                        start_ms=perp_submit.start_ms,
                        fallback_units=perp_submit.immediate_units,
                        fallback_notional=perp_submit.immediate_notional,
                    )
                    if perp_fix.units <= 0:
                        logger.warning(
                            f"Perp repair for {coin} did not fill; aborting paired filler."
                        )
                        break
                    total_perp += perp_fix.units
                    total_perp_notional += perp_fix.notional
                    pointer = self._build_order_pointer(
                        perp_submit.response,
                        reason="hyperliquid_perp_repair",
                        metadata={
                            "asset_id": perp_asset_id,
                            "size": perp_fix.units,
                            "asset_name": coin,
                            "client_id": cl_fix,
                        },
                    )
                    if pointer:
                        perp_pointers.append(pointer)
                    delta_units -= perp_fix.units
                else:
                    # More perp than spot - need more spot
                    max_units_by_cash = fix_units
                    if spot_is_buy:
                        max_units_by_cash = await self._max_spot_units(
                            fix_units,
                            mid_price,
                            slip_fraction,
                            step,
                            min_units,
                        )
                        if max_units_by_cash <= 0:
                            logger.warning(
                                f"Skipping repair buy for {coin}: insufficient USDC (need {fix_units:.4f} units)."
                            )
                            break
                    fix_units = min(fix_units, max_units_by_cash)
                    if fix_units < min_units:
                        logger.warning(
                            f"Repair size for {coin} below venue minimum after cash check; leaving residual."
                        )
                        break
                    if fix_units <= 0:
                        break
                    cl_fix = _rand_cloid()
                    spot_submit = await self._ioc_leg(
                        is_spot=True,
                        asset_id=spot_asset_id,
                        coin=coin,
                        side_is_buy=spot_is_buy,
                        units=fix_units,
                        slip_fraction=slip_fraction,
                        cloid=cl_fix,
                        builder_fee=builder_fee,
                    )
                    spot_fix = await self.confirmer.confirm_leg(
                        address=self.address,
                        coin_label=spot_submit.coin_label,
                        initial_oids=spot_submit.oids,
                        cloid=cl_fix,
                        start_ms=spot_submit.start_ms,
                        fallback_units=spot_submit.immediate_units,
                        fallback_notional=spot_submit.immediate_notional,
                    )
                    if spot_fix.units <= 0:
                        logger.warning(
                            f"Spot repair for {coin} did not fill; aborting paired filler."
                        )
                        break
                    total_spot += spot_fix.units
                    total_spot_notional += spot_fix.notional
                    delta_units += spot_fix.units
                    pointer = self._build_order_pointer(
                        spot_submit.response,
                        reason="hyperliquid_spot_repair",
                        metadata={
                            "asset_id": spot_asset_id,
                            "size": spot_fix.units,
                            "asset_name": coin,
                            "client_id": cl_fix,
                        },
                    )
                    if pointer:
                        spot_pointers.append(pointer)

                delta_units = float(Decimal(str(delta_units)))
                continue

            # Main fill loop
            loops += 1
            chunk = min(remaining, max_chunk_units)
            chunk = max(chunk, float(step))
            chunk = _round_down_units(chunk, step)
            if chunk <= 0:
                break

            is_residual = (remaining < max_chunk_units) and (loops > 1)

            if is_residual:
                shrink_factor = self.cfg.residual_shrink
                if shrink_factor <= 0.0:
                    shrink_factor = 0.90

                if shrink_factor < 1.0:
                    shrunk_chunk = _round_down_units(chunk * shrink_factor, step)
                    if shrunk_chunk < min_units:
                        logger.warning(
                            "Residual chunk {:.6f} {} shrunk to {:.6f} is below min_units {:.6f}; skipping residual and ending fill loop.",
                            chunk,
                            coin,
                            shrunk_chunk,
                            min_units,
                        )
                        break
                    logger.debug(
                        "Using residual chunk shrink for {}: remaining={:.6f}, chunk={:.6f} -> {:.6f}",
                        coin,
                        remaining,
                        chunk,
                        shrunk_chunk,
                    )
                    chunk = shrunk_chunk

            spot_valid = self.adapter.get_valid_order_size(spot_asset_id, chunk)
            perp_valid = self.adapter.get_valid_order_size(perp_asset_id, chunk)
            chunk = _round_down_units(min(spot_valid, perp_valid), step)

            if chunk <= 0:
                logger.warning(
                    "Chunk became non-positive after venue clipping for {}; aborting fill loop.",
                    coin,
                )
                break

            if chunk < min_units:
                logger.warning(
                    "Chunk invalid after venue clipping: {:.6f} {} < min_units {:.6f}; aborting fill loop.",
                    chunk,
                    coin,
                    min_units,
                )
                break

            if spot_is_buy:
                cash_limited_units = await self._max_spot_units(
                    chunk,
                    mid_price,
                    slip_fraction,
                    step,
                    min_units,
                )
                if cash_limited_units <= 0:
                    logger.warning(
                        f"Insufficient USDC available to open spot leg for {coin}; aborting fill loop."
                    )
                    break
                if cash_limited_units < chunk:
                    chunk = cash_limited_units

            cl_spot = _rand_cloid()
            cl_perp = _rand_cloid()

            # Execute spot and perp legs in parallel
            spot_task = asyncio.create_task(
                self._ioc_leg(
                    is_spot=True,
                    asset_id=spot_asset_id,
                    coin=coin,
                    side_is_buy=spot_is_buy,
                    units=chunk,
                    slip_fraction=slip_fraction,
                    cloid=cl_spot,
                    builder_fee=builder_fee,
                )
            )
            perp_task = asyncio.create_task(
                self._ioc_leg(
                    is_spot=False,
                    asset_id=perp_asset_id,
                    coin=coin,
                    side_is_buy=perp_is_buy,
                    units=chunk,
                    slip_fraction=slip_fraction,
                    cloid=cl_perp,
                    builder_fee=builder_fee,
                )
            )

            spot_submit, perp_submit = await asyncio.gather(spot_task, perp_task)
            start_ms = min(spot_submit.start_ms, perp_submit.start_ms)

            spot_fill = await self.confirmer.confirm_leg(
                address=self.address,
                coin_label=spot_submit.coin_label,
                initial_oids=spot_submit.oids,
                cloid=cl_spot,
                start_ms=start_ms,
                fallback_units=spot_submit.immediate_units,
                fallback_notional=spot_submit.immediate_notional,
            )
            perp_fill = await self.confirmer.confirm_leg(
                address=self.address,
                coin_label=perp_submit.coin_label,
                initial_oids=perp_submit.oids,
                cloid=cl_perp,
                start_ms=start_ms,
                fallback_units=perp_submit.immediate_units,
                fallback_notional=perp_submit.immediate_notional,
            )

            if spot_fill.units <= 0.0 and perp_fill.units <= 0.0:
                logger.warning("Paired filler made no progress for {}; aborting.", coin)
                break

            if (
                spot_fill.units > 0.0
                and perp_fill.units <= 0.0
                and self._is_margin_rejected(perp_submit.response)
            ):
                logger.warning(
                    "Perp leg marginRejected for {}; attempting to roll back spot leg of {:.6f} units.",
                    coin,
                    spot_fill.units,
                )
                try:
                    rollback_cl = _rand_cloid()
                    rollback_submit = await self._ioc_leg(
                        is_spot=True,
                        asset_id=spot_asset_id,
                        coin=coin,
                        side_is_buy=not spot_is_buy,
                        units=spot_fill.units,
                        slip_fraction=slip_fraction,
                        cloid=rollback_cl,
                        builder_fee=builder_fee,
                    )
                    rollback_fill = await self.confirmer.confirm_leg(
                        address=self.address,
                        coin_label=rollback_submit.coin_label,
                        initial_oids=rollback_submit.oids,
                        cloid=rollback_cl,
                        start_ms=rollback_submit.start_ms,
                        fallback_units=rollback_submit.immediate_units,
                        fallback_notional=rollback_submit.immediate_notional,
                    )
                    logger.warning(
                        "Rollback for {} completed: {:.6f} units (requested {:.6f}).",
                        coin,
                        rollback_fill.units,
                        spot_fill.units,
                    )
                except Exception as exc:
                    logger.error("Rollback spot leg for {} failed: {}", coin, exc)

                break

            if (
                perp_fill.units > 0.0
                and spot_fill.units <= 0.0
                and self._is_errorish(spot_submit.response)
            ):
                logger.warning(
                    "Spot leg failed but perp filled for {}; rolling back {:.6f} units.",
                    coin,
                    perp_fill.units,
                )
                try:
                    rollback_cl = _rand_cloid()
                    perp_rollback = await self._ioc_leg(
                        is_spot=False,
                        asset_id=perp_asset_id,
                        coin=coin,
                        side_is_buy=not perp_is_buy,
                        units=perp_fill.units,
                        slip_fraction=slip_fraction,
                        cloid=rollback_cl,
                        builder_fee=builder_fee,
                    )
                    rollback_fill = await self.confirmer.confirm_leg(
                        address=self.address,
                        coin_label=perp_rollback.coin_label,
                        initial_oids=perp_rollback.oids,
                        cloid=rollback_cl,
                        start_ms=perp_rollback.start_ms,
                        fallback_units=perp_rollback.immediate_units,
                        fallback_notional=perp_rollback.immediate_notional,
                    )
                    logger.warning(
                        "Perp rollback for {} completed: {:.6f} units (requested {:.6f}).",
                        coin,
                        rollback_fill.units,
                        perp_fill.units,
                    )
                except Exception as exc:
                    logger.error("Perp rollback for {} failed: {}", coin, exc)

                break

            total_spot += spot_fill.units
            total_perp += perp_fill.units
            total_spot_notional += spot_fill.notional
            total_perp_notional += perp_fill.notional

            spot_pointer = self._build_order_pointer(
                spot_submit.response,
                reason="hyperliquid_spot_leg",
                metadata={
                    "asset_id": spot_asset_id,
                    "size": spot_fill.units,
                    "asset_name": coin,
                    "client_id": cl_spot,
                },
            )
            if spot_pointer:
                spot_pointers.append(spot_pointer)

            perp_pointer = self._build_order_pointer(
                perp_submit.response,
                reason="hyperliquid_perp_leg",
                metadata={
                    "asset_id": perp_asset_id,
                    "size": perp_fill.units,
                    "asset_name": coin,
                    "client_id": cl_perp,
                },
            )
            if perp_pointer:
                perp_pointers.append(perp_pointer)

            progressed = min(spot_fill.units, perp_fill.units)
            remaining = max(0.0, remaining - progressed)
            remaining = _round_down_units(remaining, step)

            delta_units += spot_fill.units - perp_fill.units
            delta_units = float(Decimal(str(delta_units)))

        if abs(delta_units) >= step_float:
            logger.warning(
                "Residual mismatch after repairs for {}: spot={} perp={}",
                coin,
                total_spot,
                total_perp,
            )

        return (
            total_spot,
            total_perp,
            total_spot_notional,
            total_perp_notional,
            spot_pointers,
            perp_pointers,
        )

    @staticmethod
    def _is_margin_rejected(resp: dict[str, Any]) -> bool:
        if not isinstance(resp, dict):
            return False

        top_status = str(resp.get("status", "")).lower()
        if "margin" in top_status and "reject" in top_status:
            return True

        order = resp.get("order")
        if isinstance(order, dict):
            inner_status = str(order.get("status", "")).lower()
            if "margin" in inner_status and "reject" in inner_status:
                return True

        return False

    @staticmethod
    def _is_errorish(resp: dict[str, Any]) -> bool:
        if not isinstance(resp, dict):
            return True

        ok_statuses = {"ok", "filled"}
        status = str(resp.get("status", "")).lower()
        if status and status not in ok_statuses:
            return True

        order = resp.get("order")
        if isinstance(order, dict):
            inner_status = str(order.get("status", "")).lower()
            if inner_status and inner_status not in ok_statuses.union({"resting"}):
                return True

        return False

    async def _ioc_leg(
        self,
        *,
        is_spot: bool,
        asset_id: int,
        coin: str,
        side_is_buy: bool,
        units: float,
        slip_fraction: float,
        cloid: str,
        builder_fee: dict[str, Any] | None = None,
    ) -> LegSubmitResult:
        start_ms = _now_ms()
        rounded_units = self.adapter.get_valid_order_size(asset_id, units)
        if rounded_units <= 0:
            raise ValueError(
                f"Units {units} for {coin} rounded below venue minimum for asset {asset_id}."
            )

        success, response = await self.adapter.place_market_order(
            asset_id,
            side_is_buy,
            slip_fraction,
            rounded_units,
            self.address,
            cloid=cloid,
            builder=builder_fee,
        )
        if not success:
            response = {"status": "error", "error": response}

        oids, immediate_units, immediate_notional = _parse_oids_and_immediate_fill(
            response if isinstance(response, dict) else {}
        )
        coin_label = f"{coin}/USDC" if is_spot else coin
        return LegSubmitResult(
            oids=oids,
            start_ms=start_ms,
            coin_label=coin_label,
            immediate_units=immediate_units,
            immediate_notional=immediate_notional,
            response=response if isinstance(response, dict) else {},
        )

    async def _spot_usdc_available(self) -> float:
        try:
            success, state = await self.adapter.get_spot_user_state(self.address)
            if not success:
                return 0.0
        except Exception as exc:
            logger.info(
                f"Failed to fetch spot balances for {self.address} while sizing repairs: {exc}"
            )
            return 0.0

        balances = state.get("balances", [])
        for balance in balances:
            if balance.get("coin") == "USDC":
                try:
                    return float(balance.get("total", 0.0))
                except (TypeError, ValueError):
                    return 0.0
        return 0.0

    async def _max_spot_units(
        self,
        desired_units: float,
        mid_price: float,
        slip_fraction: float,
        step: Decimal,
        min_units: float,
    ) -> float:
        if desired_units <= 0 or mid_price <= 0:
            return 0.0
        available = await self._spot_usdc_available()
        if available <= 0:
            return 0.0
        buffer = 1.0 + slip_fraction + 0.001
        max_units = available / (mid_price * buffer)
        if max_units <= 0:
            return 0.0
        max_units = _round_down_units(max_units, step)
        if max_units < min_units:
            return 0.0
        return min(desired_units, max_units)

    def _min_units_for_notional(self, mid_price: float, step: Decimal) -> float:
        if mid_price <= 0:
            return max(float(step), 0.0)
        raw_units = MIN_NOTIONAL_USD / mid_price
        quantized = _round_up_units(raw_units, step)
        if quantized <= 0:
            return float(step)
        return max(float(step), quantized)

    def _common_step(self, spot_asset_id: int, perp_asset_id: int) -> Decimal:
        spot_decimals = self.adapter.asset_to_sz_decimals[spot_asset_id]
        perp_decimals = self.adapter.asset_to_sz_decimals[perp_asset_id]
        return Decimal(10) ** -min(spot_decimals, perp_decimals)

    @staticmethod
    def _build_order_pointer(
        response: dict[str, Any],
        reason: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not response or response.get("status") != "ok":
            return None
        return {
            "reason": reason,
            "response": response,
            "metadata": metadata,
        }
