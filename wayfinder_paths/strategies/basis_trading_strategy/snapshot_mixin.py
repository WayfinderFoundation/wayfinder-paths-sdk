from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from wayfinder_paths.core.clients.HyperliquidDataClient import HYPERLIQUID_DATA_CLIENT
from wayfinder_paths.core.clients.protocols import HyperliquidDataClientProtocol

if TYPE_CHECKING:
    from .strategy import BasisTradingStrategy


class BasisSnapshotMixin:
    def _get_hyperliquid_data_client(self: BasisTradingStrategy) -> HyperliquidDataClientProtocol:
        client = getattr(self, "_hyperliquid_data_client", None)
        if client is None:
            client = HYPERLIQUID_DATA_CLIENT
            self._hyperliquid_data_client = client
        return client

    def _build_safe_entry(
        self: BasisTradingStrategy,
        *,
        horizon: int,
        deposit_usdc: float,
        leverage: int,
        entry_mmr: float,
        margin_table_id: int | None,
        max_coin_leverage: int,
        fee_eps: float,
        mark_price: float,
        spot_asset_id: int,
        perp_asset_id: int,
        depth_checks: dict[str, dict[str, Any]],
        hourly_funding: list[float],
        closes: list[float],
        highs: list[float],
        net_apy: float,
        gross_apy: float,
        entry_cost_usd: float,
        exit_cost_usd: float,
    ) -> dict[str, Any]:
        L = max(1, int(leverage))

        depth_checks = depth_checks or {}
        depth_checks = {
            key: (value.copy() if isinstance(value, dict) else value)
            for key, value in depth_checks.items()
        }

        if mark_price <= 0 or deposit_usdc <= 0:
            return {
                "safe_leverage": 0,
                "buffer_fraction_required": 0.0,
                "per_leg_notional": 0.0,
                "spot_usdc": 0.0,
                "perp_margin_usdc": 0.0,
                "spot_amount": 0.0,
                "perp_amount": 0.0,
                "m_notional_multiple": 0.0,
                "expected_apy_before_fees": float(gross_apy),
                "note": "Insufficient inputs for sizing",
                "depth_check": depth_checks,
            }

        unit_step = self._common_unit_step(spot_asset_id, perp_asset_id)
        mark = Decimal(str(mark_price))
        N_max = Decimal(str(deposit_usdc)) * Decimal(L) / (Decimal(L) + Decimal(1))
        units = (N_max / mark / unit_step).to_integral_value(
            rounding=ROUND_DOWN
        ) * unit_step

        if units <= 0:
            return {
                "safe_leverage": 0,
                "buffer_fraction_required": 0.0,
                "per_leg_notional": 0.0,
                "spot_usdc": 0.0,
                "perp_margin_usdc": 0.0,
                "spot_amount": 0.0,
                "perp_amount": 0.0,
                "m_notional_multiple": 0.0,
                "expected_apy_before_fees": float(gross_apy),
                "note": "Deposit too small for venue lot size",
                "depth_check": depth_checks,
            }

        spot_amount_units = self.round_size_for_hypecore_asset(
            spot_asset_id, float(units), ensure_min_step=True
        )
        perp_amount_units = self.round_size_for_hypecore_asset(
            perp_asset_id, float(units), ensure_min_step=True
        )

        Nq = Decimal(str(spot_amount_units)) * mark
        spot_usdc = float(Nq)
        perp_margin_usdc = float(Nq / Decimal(L)) if L > 0 else 0.0
        m_notional = float(Decimal(L) / (Decimal(L) + Decimal(1))) if L > 0 else 0.0

        base_notional = float(Nq)
        coin_max_lev = max(1, int(max_coin_leverage) if max_coin_leverage else L)
        fallback_buffer = float(
            (
                entry_mmr
                if entry_mmr > 0
                else self.maintenance_rate_from_max_leverage(coin_max_lev)
            )
            + fee_eps
        )

        window = 24 * max(1, int(horizon))
        if (
            len(hourly_funding) <= window
            or len(closes) <= window
            or len(highs) <= window
        ):
            B_star = fallback_buffer
        else:
            B_star = float(
                self._buffer_requirement_tiered(
                    closes=closes,
                    highs=highs,
                    hourly_funding=hourly_funding,
                    window=window,
                    margin_table_id=margin_table_id,
                    base_notional=base_notional,
                    fallback_max_leverage=coin_max_lev,
                    fee_eps=fee_eps,
                    require_full_window=True,
                )
            )

        note = (
            f"Net APY {net_apy:.2%}, gross {gross_apy:.2%}, costs(entry {entry_cost_usd:.2f},"
            f" exit {exit_cost_usd:.2f})"
        )

        return {
            "safe_leverage": int(L),
            "buffer_fraction_required": float(B_star),
            "per_leg_notional": float(Nq),
            "spot_usdc": float(spot_usdc),
            "perp_margin_usdc": float(perp_margin_usdc),
            "spot_amount": float(spot_amount_units),
            "perp_amount": float(perp_amount_units),
            "m_notional_multiple": float(m_notional),
            "expected_apy_before_fees": float(gross_apy),
            "note": note,
            "depth_check": depth_checks,
        }

    async def find_best_trade_with_backtest(
        self: BasisTradingStrategy,
        *,
        deposit_usdc: float,
        stop_frac: float = 0.75,
        lookback_days: int = 180,
        oi_floor: float = 50.0,
        day_vlm_floor: float = 1e5,
        max_leverage: int = 3,
        fee_eps: float = 0.003,
        fee_model: dict[str, float] | None = None,
        depth_params: dict[str, Any] | None = None,
        perp_slippage_bps: float = 1.0,
        cooloff_hours: int = 0,
        horizons_days: list[int] | None = None,
        bootstrap_sims: int | None = None,
        bootstrap_block_hours: int | None = None,
        bootstrap_seed: int | None = None,
    ) -> dict[str, Any] | None:
        bootstrap_sims = int(
            self.DEFAULT_BOOTSTRAP_SIMS if bootstrap_sims is None else bootstrap_sims
        )
        bootstrap_block_hours = int(
            self.DEFAULT_BOOTSTRAP_BLOCK_HOURS
            if bootstrap_block_hours is None
            else bootstrap_block_hours
        )

        ranked = await self.solve_candidates_max_net_apy_with_stop(
            deposit_usdc=deposit_usdc,
            stop_frac=stop_frac,
            lookback_days=lookback_days,
            oi_floor=oi_floor,
            day_vlm_floor=day_vlm_floor,
            max_leverage=max_leverage,
            fee_eps=fee_eps,
            fee_model=fee_model,
            depth_params=depth_params,
            perp_slippage_bps=perp_slippage_bps,
            cooloff_hours=cooloff_hours,
            bootstrap_sims=bootstrap_sims,
            bootstrap_block_hours=bootstrap_block_hours,
            bootstrap_seed=bootstrap_seed,
        )
        if not ranked:
            return None

        best = dict(ranked[0])
        horizons = horizons_days or [1, 7]

        if best.get("margin_table_id"):
            await self._get_margin_table_tiers(int(best["margin_table_id"]))

        ms_now = int(time.time() * 1000)
        start_ms = ms_now - int(int(lookback_days) * 24 * 3600 * 1000)

        client = self._get_hyperliquid_data_client()
        try:
            funding_data, candle_data = await asyncio.gather(
                client.get_funding_history(best["coin"], start_ms, ms_now),
                client.get_candles(best["coin"], start_ms, ms_now),
            )
        except Exception:
            funding_data, candle_data = [], []

        hourly_funding = [float(x.get("fundingRate", 0.0)) for x in funding_data]
        closes = [float(c.get("c", 0)) for c in candle_data if c.get("c")]
        highs = [float(c.get("h", 0)) for c in candle_data if c.get("h")]

        safe_map: dict[str, dict[str, Any]] = {}
        depth_checks = best.get("depth_checks") or {}

        for horizon in horizons:
            safe_entry = self._build_safe_entry(
                horizon=horizon,
                deposit_usdc=deposit_usdc,
                leverage=int(best.get("best_L", 0) or 0),
                entry_mmr=float(best.get("mmr", 0.0) or 0.0),
                margin_table_id=best.get("margin_table_id"),
                max_coin_leverage=int(best.get("max_coin_leverage", 0) or 0),
                fee_eps=fee_eps,
                mark_price=float(best.get("mark_price", 0.0) or 0.0),
                spot_asset_id=int(best.get("spot_asset_id", 0) or 0),
                perp_asset_id=int(best.get("perp_asset_id", 0) or 0),
                depth_checks=depth_checks,
                hourly_funding=hourly_funding,
                closes=closes,
                highs=highs,
                net_apy=float(best.get("net_apy", 0.0) or 0.0),
                gross_apy=float(best.get("gross_funding_apy", 0.0) or 0.0),
                entry_cost_usd=float(best.get("entry_cost_usd", 0.0) or 0.0),
                exit_cost_usd=float(best.get("exit_cost_usd", 0.0) or 0.0),
            )
            safe_map[str(horizon)] = safe_entry

        best["safe"] = safe_map
        best["deposit_usdc"] = float(deposit_usdc)
        best["stop_frac"] = float(stop_frac)
        best["lookback_days"] = int(lookback_days)
        best["oi_floor"] = float(oi_floor)
        best["day_vlm_floor"] = float(day_vlm_floor)
        best["max_leverage_limit"] = int(max_leverage)
        best["fee_eps"] = float(fee_eps)
        best["perp_slippage_bps"] = float(perp_slippage_bps)
        best["cooloff_hours"] = int(cooloff_hours)
        best["horizons_days"] = list(horizons)

        best["backtest"] = {
            key: best[key]
            for key in (
                "coin",
                "spot_pair",
                "spot_asset_id",
                "perp_asset_id",
                "best_L",
                "net_apy",
                "gross_funding_apy",
                "entry_cost_usd",
                "exit_cost_usd",
                "cycles",
                "hit_rate_per_day",
                "avg_hold_hours",
                "time_in_market_frac",
                "stop_frac",
                "mmr",
                "margin_table_id",
                "max_coin_leverage",
                "cost_breakdown",
                "depth_checks",
            )
            if key in best
        }

        return best

    def _hour_bucket_start(self: BasisTradingStrategy, ts: datetime | None = None) -> datetime:
        now = ts or datetime.now(UTC)
        return now.replace(minute=0, second=0, microsecond=0, tzinfo=UTC)

    async def build_batch_snapshot(
        self: BasisTradingStrategy,
        *,
        score_deposit_usdc: float = 1000.0,
        stop_frac: float | None = None,
        lookback_days: int | None = None,
        oi_floor: float | None = None,
        day_vlm_floor: float | None = None,
        max_leverage: int | None = None,
        fee_eps: float | None = None,
        fee_model: dict[str, float] | None = None,
        depth_params: dict[str, Any] | None = None,
        perp_slippage_bps: float = 1.0,
        cooloff_hours: int = 0,
        coin_whitelist: list[str] | None = None,
        bootstrap_sims: int | None = None,
        bootstrap_block_hours: int | None = None,
        bootstrap_seed: int | None = None,
    ) -> dict[str, Any]:
        stop_frac = float(
            stop_frac if stop_frac is not None else self.LIQUIDATION_REBALANCE_THRESHOLD
        )
        lookback_days = int(
            lookback_days if lookback_days is not None else self.DEFAULT_LOOKBACK_DAYS
        )
        oi_floor = float(oi_floor if oi_floor is not None else self.DEFAULT_OI_FLOOR)
        day_vlm_floor = float(
            day_vlm_floor if day_vlm_floor is not None else self.DEFAULT_DAY_VLM_FLOOR
        )
        max_leverage = int(
            max_leverage if max_leverage is not None else self.DEFAULT_MAX_LEVERAGE
        )
        fee_eps = float(fee_eps if fee_eps is not None else self.DEFAULT_FEE_EPS)
        bootstrap_sims = int(
            bootstrap_sims
            if bootstrap_sims is not None
            else (self._cfg_get("bootstrap_sims", self.DEFAULT_BOOTSTRAP_SIMS) or 0)
        )
        bootstrap_block_hours = int(
            bootstrap_block_hours
            if bootstrap_block_hours is not None
            else (
                self._cfg_get(
                    "bootstrap_block_hours", self.DEFAULT_BOOTSTRAP_BLOCK_HOURS
                )
                or 0
            )
        )
        if bootstrap_seed is None:
            cfg_seed = self._cfg_get("bootstrap_seed")
            bootstrap_seed = int(cfg_seed) if cfg_seed is not None else None
        else:
            bootstrap_seed = int(bootstrap_seed)

        whitelist = (
            {coin.upper() for coin in coin_whitelist} if coin_whitelist else None
        )

        (
            success,
            perps_ctx_pack,
        ) = await self.hyperliquid_adapter.get_meta_and_asset_ctxs()
        if not success:
            raise ValueError(f"Failed to fetch perp metadata: {perps_ctx_pack}")

        perps_meta_list = perps_ctx_pack[0]["universe"]
        perps_ctxs = perps_ctx_pack[1]

        coin_to_ctx: dict[str, Any] = {}
        coin_to_maxlev: dict[str, int] = {}
        coin_to_margin_table: dict[str, int | None] = {}
        coins: list[str] = []
        for meta, ctx in zip(perps_meta_list, perps_ctxs, strict=False):
            coin = meta["name"]
            coin_to_ctx[coin] = ctx
            coin_to_maxlev[coin] = int(meta.get("maxLeverage", 10))
            coin_to_margin_table[coin] = meta.get("marginTableId")
            coins.append(coin)
        perps_set = set(coins)

        perp_coin_to_asset_id = {
            k: v for k, v in self.hyperliquid_adapter.coin_to_asset.items() if v < 10000
        }

        success, spot_meta = await self.hyperliquid_adapter.get_spot_meta()
        if not success:
            raise ValueError(f"Failed to fetch spot metadata: {spot_meta}")

        tokens = spot_meta.get("tokens", [])
        spot_pairs = spot_meta.get("universe", [])
        idx_to_token = {t["index"]: t["name"] for t in tokens}

        candidates = self._find_basis_candidates(spot_pairs, idx_to_token, perps_set)

        ms_now = int(time.time() * 1000)
        start_ms = ms_now - int(lookback_days * 24 * 3600 * 1000)

        snapshot_candidates: list[dict[str, Any]] = []

        for spot_sym, coin, spot_asset_id in candidates:
            if whitelist is not None and coin.upper() not in whitelist:
                continue

            ctx = coin_to_ctx.get(coin, {})
            oi_base = float(ctx.get("openInterest") or 0.0)
            mark_px = float(ctx.get("markPx") or 0.0)
            day_ntl_usd = float(ctx.get("dayNtlVlm") or 0.0)
            if mark_px <= 0:
                continue

            perp_asset_id = perp_coin_to_asset_id.get(coin)
            if perp_asset_id is None:
                continue

            oi_usd = oi_base * mark_px
            if oi_usd < oi_floor or day_ntl_usd < day_vlm_floor:
                continue

            raw_max_lev = coin_to_maxlev.get(coin, max_leverage)
            coin_max_lev = int(raw_max_lev) if raw_max_lev else max_leverage
            max_available_lev = max(1, min(max_leverage, coin_max_lev))
            margin_table_id = coin_to_margin_table.get(coin)

            try:
                spot_book = await self._l2_book_spot(
                    spot_asset_id, fallback_mid=mark_px, spot_symbol=spot_sym
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(f"Skipping {spot_sym}: L2 fetch error: {exc}")
                continue

            cap = await self.max_spot_order_usd_for_book(
                spot_asset_id=spot_asset_id,
                spot_symbol=spot_sym,
                book=spot_book,
                day_ntl_usd=day_ntl_usd,
                params=depth_params,
            )
            max_order_usd = float(cap.get("max_order_usd") or 0.0)
            if max_order_usd <= 0.0:
                continue

            if margin_table_id:
                await self._get_margin_table_tiers(int(margin_table_id))

            client = self._get_hyperliquid_data_client()
            try:
                funding_data, candle_data = await asyncio.gather(
                    client.get_funding_history(coin, start_ms, ms_now),
                    client.get_candles(coin, start_ms, ms_now),
                )
            except Exception:
                continue

            hourly_funding = [float(x.get("fundingRate", 0.0)) for x in funding_data]
            closes = [float(c.get("c", 0)) for c in candle_data if c.get("c")]
            highs = [float(c.get("h", 0)) for c in candle_data if c.get("h")]

            n_ok = min(len(hourly_funding), len(closes), len(highs))
            if n_ok < (lookback_days * 24 - 48):
                continue

            options: list[dict[str, Any]] = []

            for L in range(1, max_available_lev + 1):
                deposit_min = self._min_deposit_needed(
                    mark_price=mark_px,
                    leverage=L,
                    spot_asset_id=spot_asset_id,
                    perp_asset_id=perp_asset_id,
                )
                deposit_max = max_order_usd * (float(L) + 1.0) / float(L)
                if deposit_max < deposit_min:
                    continue

                # Use a small safety factor when scoring at the top of capacity to
                # avoid borderline pass/fail due to book rounding / float noise.
                deposit_max_safe = deposit_max * 0.98
                score_dep = min(float(score_deposit_usdc), float(deposit_max_safe))
                if score_dep < deposit_min:
                    score_dep = float(deposit_min)

                order_usd = score_dep * (float(L) / (float(L) + 1.0))

                entry_mmr = self.maintenance_fraction_for_notional(
                    margin_table_id,
                    order_usd,
                    max_available_lev,
                )

                (
                    entry_cost,
                    exit_cost,
                    cost_breakdown,
                    depth_checks,
                ) = await self._estimate_cycle_costs(
                    N_leg_usd=order_usd,
                    spot_asset_id=spot_asset_id,
                    spot_book=spot_book,
                    fee_model=fee_model,
                    depth_params=depth_params,
                    perp_slippage_bps=perp_slippage_bps,
                    day_ntl_usd=day_ntl_usd,
                    spot_symbol=spot_sym,
                )

                # Ensure scoring uses a passing depth state. If it doesn't, shrink.
                if not (
                    bool((depth_checks.get("buy") or {}).get("pass"))
                    and bool((depth_checks.get("sell") or {}).get("pass"))
                ):
                    # Fall back to a smaller, clearly safe order size.
                    order_usd = min(order_usd, max_order_usd * 0.5)
                    score_dep = order_usd * (float(L) + 1.0) / float(L)
                    (
                        entry_cost,
                        exit_cost,
                        cost_breakdown,
                        depth_checks,
                    ) = await self._estimate_cycle_costs(
                        N_leg_usd=order_usd,
                        spot_asset_id=spot_asset_id,
                        spot_book=spot_book,
                        fee_model=fee_model,
                        depth_params=depth_params,
                        perp_slippage_bps=perp_slippage_bps,
                        day_ntl_usd=day_ntl_usd,
                        spot_symbol=spot_sym,
                    )

                sim = self._simulate_barrier_backtest(
                    funding=hourly_funding,
                    closes=closes,
                    highs=highs,
                    leverage=L,
                    stop_frac=stop_frac,
                    fee_eps=fee_eps,
                    N_leg_usd=order_usd,
                    entry_cost_usd=entry_cost,
                    exit_cost_usd=exit_cost,
                    margin_table_id=margin_table_id,
                    fallback_max_leverage=max_available_lev,
                    cooloff_hours=cooloff_hours,
                )

                hours = max(1.0, float(sim["hours"]))
                years = hours / (24.0 * 365.0)
                net_apy = (float(sim["net_pnl_usd"]) / max(1e-9, score_dep)) / years
                gross_apy = (
                    float(sim["gross_funding_usd"]) / max(1e-9, score_dep)
                ) / years
                hit_rate_per_day = (
                    float(sim["cycles"]) / (hours / 24.0) if hours > 0 else 0.0
                )
                avg_hold_hours = (
                    float(sim["hours_in_market"]) / max(1.0, float(sim["cycles"]))
                    if float(sim["cycles"]) > 0
                    else hours
                )
                time_in_market = float(sim["hours_in_market"]) / hours

                bootstrap_stats = self._bootstrap_churn_metrics(
                    funding=hourly_funding,
                    closes=closes,
                    highs=highs,
                    leverage=L,
                    stop_frac=stop_frac,
                    fee_eps=fee_eps,
                    N_leg_usd=order_usd,
                    entry_cost_usd=entry_cost,
                    exit_cost_usd=exit_cost,
                    margin_table_id=margin_table_id,
                    fallback_max_leverage=max_available_lev,
                    cooloff_hours=cooloff_hours,
                    deposit_usdc=score_dep,
                    sims=bootstrap_sims,
                    block_hours=bootstrap_block_hours,
                    seed=None
                    if bootstrap_seed is None
                    else hash((bootstrap_seed, coin, L)),
                )

                opt: dict[str, Any] = {
                    "leverage": int(L),
                    "deposit_used_usdc": float(score_dep),
                    "deposit_min_usdc": float(deposit_min),
                    "deposit_max_usdc": float(deposit_max),
                    "order_usd": float(order_usd),
                    "net_apy": float(net_apy),
                    "gross_funding_apy": float(gross_apy),
                    "entry_cost_usd": float(entry_cost),
                    "exit_cost_usd": float(exit_cost),
                    "cycles": float(sim["cycles"]),
                    "hit_rate_per_day": float(hit_rate_per_day),
                    "avg_hold_hours": float(avg_hold_hours),
                    "time_in_market_frac": float(time_in_market),
                    "stop_frac": float(stop_frac),
                    "mmr": float(entry_mmr),
                    "margin_table_id": margin_table_id,
                    "max_coin_leverage": int(max_available_lev),
                    "cost_breakdown": cost_breakdown,
                    "depth_checks": depth_checks,
                    "perp_asset_id": int(perp_asset_id),
                    "spot_asset_id": int(spot_asset_id),
                    "mark_price": float(mark_px),
                }
                if bootstrap_stats is not None:
                    opt["bootstrap_metrics"] = bootstrap_stats

                options.append(opt)

            if not options:
                continue

            best_opt = max(
                options, key=lambda o: float(o.get("net_apy", float("-inf")))
            )

            snapshot_candidates.append(
                {
                    "coin": coin,
                    "spot_pair": spot_sym,
                    "spot_asset_id": int(spot_asset_id),
                    "perp_asset_id": int(perp_asset_id),
                    "mark_price": float(mark_px),
                    "open_interest_usd": float(oi_usd),
                    "day_notional_usd": float(day_ntl_usd),
                    "margin_table_id": margin_table_id,
                    "max_coin_leverage": int(max_available_lev),
                    "liquidity": {
                        "max_order_usd": float(max_order_usd),
                        "upper_bound_usd": float(cap.get("upper_bound_usd") or 0.0),
                        "checks_at_capacity": cap.get("checks"),
                        "depth_params": depth_params or None,
                    },
                    "options": options,
                    "best": best_opt,
                }
            )

        snapshot_candidates.sort(
            key=lambda c: float((c.get("best") or {}).get("net_apy", float("-inf"))),
            reverse=True,
        )

        bucket = self._hour_bucket_start()
        return {
            "kind": "basis_trading_batch_snapshot",
            "generated_at": int(time.time() * 1000),
            "hour_bucket_utc": bucket.isoformat(),
            "params": {
                "score_deposit_usdc": float(score_deposit_usdc),
                "stop_frac": float(stop_frac),
                "lookback_days": int(lookback_days),
                "oi_floor": float(oi_floor),
                "day_vlm_floor": float(day_vlm_floor),
                "max_leverage": int(max_leverage),
                "fee_eps": float(fee_eps),
                "fee_model": fee_model or None,
                "depth_params": depth_params or None,
                "perp_slippage_bps": float(perp_slippage_bps),
                "cooloff_hours": int(cooloff_hours),
                "bootstrap_sims": int(bootstrap_sims),
                "bootstrap_block_hours": int(bootstrap_block_hours),
                "bootstrap_seed": bootstrap_seed,
                "coin_whitelist": sorted(whitelist) if whitelist is not None else None,
            },
            "candidates": snapshot_candidates,
        }

    def opportunities_from_snapshot(
        self: BasisTradingStrategy,
        *,
        snapshot: dict[str, Any],
        deposit_usdc: float,
    ) -> list[dict[str, Any]]:
        if deposit_usdc <= 0:
            return []

        candidates = snapshot.get("candidates", [])
        if not isinstance(candidates, list):
            return []

        opportunities: list[dict[str, Any]] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            options = candidate.get("options", [])
            if not isinstance(options, list):
                continue

            feasible: list[dict[str, Any]] = []
            for opt in options:
                if not isinstance(opt, dict):
                    continue
                dep_min = float(opt.get("deposit_min_usdc", 0.0) or 0.0)
                dep_max = float(opt.get("deposit_max_usdc", 0.0) or 0.0)
                if dep_min <= float(deposit_usdc) <= dep_max:
                    feasible.append(opt)

            if not feasible:
                continue

            best_opt = max(
                feasible, key=lambda o: float(o.get("net_apy", float("-inf")))
            )
            L = int(best_opt.get("leverage", 1) or 1)
            order_usd = float(deposit_usdc) * (float(L) / (float(L) + 1.0))
            out = {
                "coin": candidate.get("coin"),
                "spot_pair": candidate.get("spot_pair"),
                "spot_asset_id": candidate.get("spot_asset_id"),
                "perp_asset_id": candidate.get("perp_asset_id"),
                "mark_price": candidate.get("mark_price"),
                "open_interest_usd": candidate.get("open_interest_usd"),
                "day_notional_usd": candidate.get("day_notional_usd"),
                "liquidity": candidate.get("liquidity"),
                "selection": dict(best_opt),
                "deposit_usdc": float(deposit_usdc),
                "order_usd": float(order_usd),
            }
            opportunities.append(out)

        opportunities.sort(
            key=lambda o: float(
                (o.get("selection") or {}).get("net_apy", float("-inf"))
            ),
            reverse=True,
        )
        return opportunities

    async def score_opportunity_from_snapshot(
        self: BasisTradingStrategy,
        *,
        opportunity: dict[str, Any],
        deposit_usdc: float,
        horizons_days: list[int] | None = None,
        stop_frac: float = 0.75,
        lookback_days: int = 45,
        fee_eps: float = 0.003,
        fee_model: dict[str, float] | None = None,
        depth_params: dict[str, Any] | None = None,
        perp_slippage_bps: float = 1.0,
        cooloff_hours: int = 0,
        bootstrap_sims: int = 0,
        bootstrap_block_hours: int = 48,
        bootstrap_seed: int | None = None,
    ) -> dict[str, Any] | None:
        if deposit_usdc <= 0:
            return None

        coin = opportunity.get("coin")
        spot_pair = opportunity.get("spot_pair")
        spot_asset_id = opportunity.get("spot_asset_id")
        perp_asset_id = opportunity.get("perp_asset_id")
        if not isinstance(coin, str) or not coin:
            return None
        if not isinstance(spot_pair, str) or not spot_pair:
            spot_pair = coin
        if not isinstance(spot_asset_id, int) or not isinstance(perp_asset_id, int):
            return None

        selection = opportunity.get("selection") or {}
        if not isinstance(selection, dict):
            selection = {}
        L = int(selection.get("leverage") or selection.get("best_L") or 1)
        L = max(1, L)

        day_ntl_usd = float(opportunity.get("day_notional_usd", 0.0) or 0.0)
        mark_price = float(opportunity.get("mark_price", 0.0) or 0.0)
        margin_table_id = opportunity.get("margin_table_id")
        margin_table_id = int(margin_table_id) if margin_table_id is not None else None
        max_coin_leverage = int(opportunity.get("max_coin_leverage", L) or L)

        bootstrap_seed_val: int | None
        if bootstrap_seed is None:
            bootstrap_seed_val = None
        else:
            bootstrap_seed_val = int(bootstrap_seed)

        # Refresh book for precise sizing + costs
        spot_book = await self._l2_book_spot(
            spot_asset_id, fallback_mid=mark_price or None, spot_symbol=spot_pair
        )

        order_usd = float(deposit_usdc) * (float(L) / (float(L) + 1.0))

        (
            entry_cost,
            exit_cost,
            cost_breakdown,
            depth_checks,
        ) = await self._estimate_cycle_costs(
            N_leg_usd=order_usd,
            spot_asset_id=spot_asset_id,
            spot_book=spot_book,
            fee_model=fee_model,
            depth_params=depth_params,
            perp_slippage_bps=perp_slippage_bps,
            day_ntl_usd=day_ntl_usd if day_ntl_usd > 0 else None,
            spot_symbol=spot_pair,
        )

        if not (
            bool((depth_checks.get("buy") or {}).get("pass"))
            and bool((depth_checks.get("sell") or {}).get("pass"))
        ):
            return None

        if margin_table_id:
            await self._get_margin_table_tiers(int(margin_table_id))

        ms_now = int(time.time() * 1000)
        start_ms = ms_now - int(int(lookback_days) * 24 * 3600 * 1000)

        client = self._get_hyperliquid_data_client()
        try:
            funding_data, candle_data = await asyncio.gather(
                client.get_funding_history(coin, start_ms, ms_now),
                client.get_candles(coin, start_ms, ms_now),
            )
        except Exception:
            return None

        hourly_funding = [float(x.get("fundingRate", 0.0)) for x in funding_data]
        closes = [float(c.get("c", 0)) for c in candle_data if c.get("c")]
        highs = [float(c.get("h", 0)) for c in candle_data if c.get("h")]

        n_ok = min(len(hourly_funding), len(closes), len(highs))
        if n_ok < (int(lookback_days) * 24 - 48):
            return None

        entry_mmr = self.maintenance_fraction_for_notional(
            margin_table_id,
            order_usd,
            max_coin_leverage,
        )

        sim = self._simulate_barrier_backtest(
            funding=hourly_funding,
            closes=closes,
            highs=highs,
            leverage=L,
            stop_frac=stop_frac,
            fee_eps=fee_eps,
            N_leg_usd=order_usd,
            entry_cost_usd=entry_cost,
            exit_cost_usd=exit_cost,
            margin_table_id=margin_table_id,
            fallback_max_leverage=max_coin_leverage,
            cooloff_hours=cooloff_hours,
        )

        hours = max(1.0, float(sim["hours"]))
        years = hours / (24.0 * 365.0)
        net_apy = (float(sim["net_pnl_usd"]) / max(1e-9, float(deposit_usdc))) / years
        gross_apy = (
            float(sim["gross_funding_usd"]) / max(1e-9, float(deposit_usdc))
        ) / years
        hit_rate_per_day = float(sim["cycles"]) / (hours / 24.0) if hours > 0 else 0.0
        avg_hold_hours = (
            float(sim["hours_in_market"]) / max(1.0, float(sim["cycles"]))
            if float(sim["cycles"]) > 0
            else hours
        )
        time_in_market = float(sim["hours_in_market"]) / hours

        bootstrap_stats = self._bootstrap_churn_metrics(
            funding=hourly_funding,
            closes=closes,
            highs=highs,
            leverage=L,
            stop_frac=stop_frac,
            fee_eps=fee_eps,
            N_leg_usd=order_usd,
            entry_cost_usd=entry_cost,
            exit_cost_usd=exit_cost,
            margin_table_id=margin_table_id,
            fallback_max_leverage=max_coin_leverage,
            cooloff_hours=cooloff_hours,
            deposit_usdc=float(deposit_usdc),
            sims=int(bootstrap_sims),
            block_hours=int(bootstrap_block_hours),
            seed=None
            if bootstrap_seed_val is None
            else hash((bootstrap_seed_val, coin, L, int(deposit_usdc))),
        )

        horizons = horizons_days or [1, 7]
        safe_map: dict[str, dict[str, Any]] = {}
        for horizon in horizons:
            safe_map[str(horizon)] = self._build_safe_entry(
                horizon=int(horizon),
                deposit_usdc=float(deposit_usdc),
                leverage=L,
                entry_mmr=float(entry_mmr),
                margin_table_id=margin_table_id,
                max_coin_leverage=max_coin_leverage,
                fee_eps=float(fee_eps),
                mark_price=float(mark_price),
                spot_asset_id=int(spot_asset_id),
                perp_asset_id=int(perp_asset_id),
                depth_checks=depth_checks,
                hourly_funding=hourly_funding,
                closes=closes,
                highs=highs,
                net_apy=float(net_apy),
                gross_apy=float(gross_apy),
                entry_cost_usd=float(entry_cost),
                exit_cost_usd=float(exit_cost),
            )

        out: dict[str, Any] = {
            "coin": coin,
            "spot_pair": spot_pair,
            "spot_asset_id": int(spot_asset_id),
            "perp_asset_id": int(perp_asset_id),
            "best_L": int(L),
            "net_apy": float(net_apy),
            "gross_funding_apy": float(gross_apy),
            "entry_cost_usd": float(entry_cost),
            "exit_cost_usd": float(exit_cost),
            "cycles": float(sim["cycles"]),
            "hit_rate_per_day": float(hit_rate_per_day),
            "avg_hold_hours": float(avg_hold_hours),
            "time_in_market_frac": float(time_in_market),
            "stop_frac": float(stop_frac),
            "mmr": float(entry_mmr),
            "margin_table_id": margin_table_id,
            "max_coin_leverage": int(max_coin_leverage),
            "cost_breakdown": cost_breakdown,
            "depth_checks": depth_checks,
            "mark_price": float(mark_price),
            "safe": safe_map,
            "deposit_usdc": float(deposit_usdc),
            "lookback_days": int(lookback_days),
            "fee_eps": float(fee_eps),
            "perp_slippage_bps": float(perp_slippage_bps),
            "cooloff_hours": int(cooloff_hours),
            "horizons_days": list(horizons),
        }
        if bootstrap_stats is not None:
            out["bootstrap_metrics"] = bootstrap_stats

        out["backtest"] = {
            key: out[key]
            for key in (
                "coin",
                "spot_pair",
                "spot_asset_id",
                "perp_asset_id",
                "best_L",
                "net_apy",
                "gross_funding_apy",
                "entry_cost_usd",
                "exit_cost_usd",
                "cycles",
                "hit_rate_per_day",
                "avg_hold_hours",
                "time_in_market_frac",
                "stop_frac",
                "mmr",
                "margin_table_id",
                "max_coin_leverage",
                "cost_breakdown",
                "depth_checks",
            )
            if key in out
        }

        return out

    def load_snapshot_from_path(self: BasisTradingStrategy, snapshot_path: str) -> dict[str, Any]:
        p = Path(snapshot_path)
        raw = p.read_text()
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("Snapshot file must contain a JSON object")
        return data

    def _snapshot_from_config(self: BasisTradingStrategy) -> dict[str, Any] | None:
        val = (
            self._cfg_get("basis_snapshot")
            or self._cfg_get("precomputed_basis_snapshot")
            or self._cfg_get("precomputed_snapshot")
        )
        return val if isinstance(val, dict) else None

    def _snapshot_path_from_config(self: BasisTradingStrategy) -> str | None:
        val = (
            self._cfg_get("basis_snapshot_path")
            or self._cfg_get("precomputed_snapshot_path")
            or self._cfg_get("precomputed_basis_snapshot_path")
        )
        if isinstance(val, str) and val.strip():
            return val.strip()
        return None
