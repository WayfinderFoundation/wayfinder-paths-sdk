from __future__ import annotations

import statistics
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from loguru import logger

from wayfinder_paths.adapters.balance_adapter.adapter import BalanceAdapter
from wayfinder_paths.adapters.projectx_adapter.adapter import ProjectXLiquidityAdapter
from wayfinder_paths.adapters.token_adapter.adapter import TokenAdapter
from wayfinder_paths.core.constants.chains import CHAIN_EXPLORER_URLS, CHAIN_ID_HYPEREVM
from wayfinder_paths.core.constants.projectx import THBILL_USDC_METADATA
from wayfinder_paths.core.strategies.descriptors import (
    DEFAULT_TOKEN_REWARDS,
    Complexity,
    Directionality,
    Frequency,
    StratDescriptor,
    TokenExposure,
    Volatility,
)
from wayfinder_paths.core.strategies.Strategy import StatusDict, StatusTuple, Strategy
from wayfinder_paths.core.utils.uniswap_v3_math import (
    amounts_for_liq_inrange,
    liq_for_amounts,
    round_tick_to_spacing,
    sqrt_price_x96_from_tick,
    sqrt_price_x96_to_price,
)
from wayfinder_paths.core.utils.units import from_erc20_raw
from wayfinder_paths.policies.erc20 import erc20_spender_for_any_token
from wayfinder_paths.policies.prjx import prjx_npm, prjx_swap

USDC_TOKEN_ID = "usd-coin-hyperevm"
THBILL_TOKEN_ID = "theo-short-duration-us-treasury-fund-hyperevm"
GAS_TOKEN_ID = "hype-hyperevm"

IDLE_REDEPLOY_THRESHOLD_USD = 0.25
MIN_AUTOCOLLECT_USD = 0.20
MIN_AUTOCOLLECT_PCT = 0.1  # 0.1% of position value


class ProjectXThbillUsdcStrategy(Strategy):
    name = "ProjectX THBILL/USDC Concentrated LP"

    GAS_THRESHOLD = 0.05
    GAS_MAXIMUM = 0.25

    # Lowered vs legacy (was 20) to enable small end-to-end runs.
    MINIMUM_NET_DEPOSIT = 5.0

    # Optional tick anchoring from swap history (requires subgraph URL)
    HISTORIC_CENTER_WINDOW_SEC = 24 * 60 * 60  # 24 hours
    HISTORIC_CENTER_MIN_SWAPS = 20

    RECENTER_WINDOW_SEC = 60 * 60  # 1 hour
    RECENTER_MIN_SWAPS = 10
    RECENTER_OUTSIDE_FRACTION = 0.8

    QUOTE_FEE_WINDOW_SEC = 24 * 60 * 60  # 24 hours
    QUOTE_FEE_MAX_SWAPS = 1000

    INFO = StratDescriptor(
        description=(
            "Concentrated-liquidity market making on ProjectX (HyperEVM) for the THBILL/USDC stable pair.\n\n"
            "Pulls HyperEVM USDC from the main wallet into the strategy wallet, swaps into the optimal split "
            "between USDC and THBILL, and provides concentrated liquidity in a tight tick band. On updates it compounds fees "
            "and recenters the band if price exits the range.\n\n"
            "Gas is paid in native HYPE on HyperEVM."
        ),
        summary="THBILL/USDC concentrated LP on ProjectX (HyperEVM) with fee compounding and band recenters.",
        risk_description=(
            "Protocol + smart contract risk (ProjectX periphery/pool, tokens), potential depeg risk, "
            "and execution risk (slippage, congestion)."
        ),
        gas_token_symbol="HYPE",
        gas_token_id=GAS_TOKEN_ID,
        deposit_token_id=USDC_TOKEN_ID,
        minimum_net_deposit=MINIMUM_NET_DEPOSIT,
        gas_maximum=GAS_MAXIMUM,
        gas_threshold=GAS_THRESHOLD,
        volatility=Volatility.LOW,
        volatility_description="Stable/stable pair; NAV near $1 unless THBILL depegs.",
        directionality=Directionality.MARKET_NEUTRAL,
        directionality_description="Concentrated liquidity on both sides of the stable pair.",
        complexity=Complexity.MEDIUM,
        complexity_description="Requires swaps + Uniswap v3 math + periodic rebalances.",
        token_exposure=TokenExposure.STABLECOINS,
        token_exposure_description="Exposure split between USDC and THBILL (USD-like).",
        frequency=Frequency.MEDIUM,
        frequency_description="Call update periodically to compound fees and recenter if out of range.",
        return_drivers=["pool fees"],
        available_rewards={
            "token_rewards": DEFAULT_TOKEN_REWARDS,
            "point_rewards": (
                [
                    {
                        "program": str(THBILL_USDC_METADATA.get("points_program")),
                        "description": "Theo points accrued via THBILL volume",
                    }
                ]
                if THBILL_USDC_METADATA.get("points_program")
                else None
            ),
        },
        config={
            "deposit": {
                "parameters": {
                    "main_token_amount": {
                        "type": "float",
                        "description": "USDC amount (usd-coin-hyperevm) to deposit from main wallet",
                    },
                    "gas_token_amount": {
                        "type": "float",
                        "description": "HYPE amount (hype-hyperevm) to deposit for gas",
                        "minimum": 0,
                        "maximum": GAS_MAXIMUM,
                    },
                }
            }
        },
    )

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        main_wallet: dict[str, Any] | None = None,
        strategy_wallet: dict[str, Any] | None = None,
        main_wallet_signing_callback: Callable[[dict], Awaitable[str]] | None = None,
        strategy_wallet_signing_callback: Callable[[dict], Awaitable[str]]
        | None = None,
        strategy_sign_typed_data: Callable[[dict], Awaitable[str]] | None = None,
    ) -> None:
        super().__init__(
            main_wallet_signing_callback=main_wallet_signing_callback,
            strategy_wallet_signing_callback=strategy_wallet_signing_callback,
            strategy_sign_typed_data=strategy_sign_typed_data,
        )

        merged_config: dict[str, Any] = dict(config or {})
        if main_wallet is not None:
            merged_config["main_wallet"] = main_wallet
        if strategy_wallet is not None:
            merged_config["strategy_wallet"] = strategy_wallet
        self.config: dict[str, Any] = merged_config

        adapter_config = {
            "main_wallet": self.config.get("main_wallet") or None,
            "strategy_wallet": self.config.get("strategy_wallet") or None,
            "strategy": self.config,
            "pool_address": str(THBILL_USDC_METADATA["pool"]),
        }
        strat_addr = (self.config.get("strategy_wallet") or {}).get("address")
        main_addr = (self.config.get("main_wallet") or {}).get("address")

        self.balance_adapter = BalanceAdapter(
            adapter_config,
            main_sign_callback=self.main_wallet_signing_callback,
            sign_callback=self.strategy_wallet_signing_callback,
            main_wallet_address=main_addr,
            wallet_address=strat_addr,
        )
        self.token_adapter = TokenAdapter()
        self.projectx = ProjectXLiquidityAdapter(
            adapter_config,
            sign_callback=self.strategy_wallet_signing_callback,
            wallet_address=strat_addr,
        )

        self.usdc_token_info: dict[str, Any] = {}
        self.thbill_token_info: dict[str, Any] = {}
        self.hype_token_info: dict[str, Any] = {}

    async def setup(self) -> None:
        self.usdc_token_info = await self._safe_get_token(USDC_TOKEN_ID)
        self.thbill_token_info = await self._safe_get_token(THBILL_TOKEN_ID)
        self.hype_token_info = await self._safe_get_token(GAS_TOKEN_ID)

    async def _safe_get_token(self, token_id: str) -> dict[str, Any]:
        try:
            ok, info = await self.token_adapter.get_token(token_id, chain_id=999)
            return info if ok and isinstance(info, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _band_ticks(current_tick: int, tick_spacing: int) -> tuple[int, int]:
        span = max(tick_spacing * 2, int(float(THBILL_USDC_METADATA["band_bps"])))
        half = (span // tick_spacing) * tick_spacing // 2
        tick_lower = round_tick_to_spacing(current_tick - half, tick_spacing)
        tick_upper = round_tick_to_spacing(current_tick + half, tick_spacing)
        if tick_lower >= tick_upper:
            tick_upper = tick_lower + tick_spacing
        return tick_lower, tick_upper

    async def _recent_ticks(
        self, *, window_sec: int, min_swaps: int, max_swaps: int = 500
    ) -> list[int]:
        now = int(time.time())
        ok, swaps = await self.projectx.fetch_swaps(
            limit=max_swaps,
            start_timestamp=now - int(window_sec),
            end_timestamp=now,
        )
        if not ok or not isinstance(swaps, list):
            return []
        ticks = [int(s["tick"]) for s in swaps if s.get("tick") is not None]
        if len(ticks) < int(min_swaps):
            return []
        return ticks

    async def _anchored_center_tick(self, fallback_tick: int, tick_spacing: int) -> int:
        ticks = await self._recent_ticks(
            window_sec=self.HISTORIC_CENTER_WINDOW_SEC,
            min_swaps=self.HISTORIC_CENTER_MIN_SWAPS,
        )
        if not ticks:
            return int(fallback_tick)

        ticks.sort()
        n = len(ticks)
        trim = max(0, int(n * 0.2))
        core = ticks[trim : n - trim] if n - 2 * trim > 0 else ticks

        center_raw = int(statistics.median(core))
        center = round_tick_to_spacing(center_raw, tick_spacing)

        max_jump = 5 * int(tick_spacing)
        if abs(center - int(fallback_tick)) > max_jump:
            center = int(fallback_tick) + max_jump * (
                1 if center > fallback_tick else -1
            )
        return int(center)

    async def _should_recentre(self, pos, *, spacing: int, current_tick: int) -> bool:
        ticks = await self._recent_ticks(
            window_sec=self.RECENTER_WINDOW_SEC,
            min_swaps=self.RECENTER_MIN_SWAPS,
        )
        if not ticks:
            return (
                current_tick <= pos.tick_lower - spacing
                or current_tick >= pos.tick_upper + spacing
            )

        outside = [t for t in ticks if t <= pos.tick_lower or t >= pos.tick_upper]
        frac_outside = len(outside) / len(ticks)
        if frac_outside < self.RECENTER_OUTSIDE_FRACTION:
            return False

        anchored_center = await self._anchored_center_tick(current_tick, spacing)
        new_lower, new_upper = self._band_ticks(anchored_center, spacing)
        if new_lower == pos.tick_lower and new_upper == pos.tick_upper:
            return False
        return True

    @staticmethod
    def _tx_link(tx_hash: str) -> str:
        base = CHAIN_EXPLORER_URLS.get(CHAIN_ID_HYPEREVM, "https://hyperevmscan.io/")
        if not base.endswith("/"):
            base += "/"
        return f"{base}tx/{tx_hash}"

    @staticmethod
    def _format_spent_summary(
        spend_meta: dict[str, int] | None,
        token0_meta: dict[str, Any],
        token1_meta: dict[str, Any],
    ) -> str:
        if not spend_meta:
            return ""

        def _fmt(amount_wei: int, meta: dict[str, Any]) -> str | None:
            if not amount_wei or amount_wei <= 0:
                return None
            decimals = int(meta.get("decimals", 18))
            symbol = meta.get("symbol") or meta.get("token_id") or "token"
            scale = Decimal(10) ** decimals
            amount = (Decimal(amount_wei) / scale).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
            if amount <= 0:
                return None
            return f"{amount} {symbol}"

        parts = list(
            filter(
                None,
                [
                    _fmt(int(spend_meta.get("token0_spent", 0) or 0), token0_meta),
                    _fmt(int(spend_meta.get("token1_spent", 0) or 0), token1_meta),
                ],
            )
        )
        if not parts:
            return ""
        return "Used " + " + ".join(parts)

    async def _token_price_usd(self, token_id: str) -> float:
        try:
            ok, price_data = await self.token_adapter.get_token_price(
                token_id, chain_id=999
            )
            if not ok or not isinstance(price_data, dict):
                return 1.0
            price = price_data.get("current_price")
            return float(price) if price is not None else 1.0
        except Exception:
            return 1.0

    async def _position_value_usd(self, pos, overview: dict[str, Any]) -> float:
        sqrt_p = int(overview.get("sqrt_price_x96") or 0)
        token0_meta = overview["token0"]
        token1_meta = overview["token1"]

        sqrt_pl = sqrt_price_x96_from_tick(int(pos.tick_lower))
        sqrt_pu = sqrt_price_x96_from_tick(int(pos.tick_upper))
        amt0, amt1 = amounts_for_liq_inrange(
            sqrt_p, sqrt_pl, sqrt_pu, int(pos.liquidity)
        )

        dec0 = int(token0_meta.get("decimals", 18))
        dec1 = int(token1_meta.get("decimals", 18))
        amt0_tokens = from_erc20_raw(amt0, dec0)
        amt1_tokens = from_erc20_raw(amt1, dec1)

        price0 = await self._token_price_usd(
            token0_meta.get("token_id") or USDC_TOKEN_ID
        )
        price1 = await self._token_price_usd(
            token1_meta.get("token_id") or THBILL_TOKEN_ID
        )
        return amt0_tokens * price0 + amt1_tokens * price1

    async def _idle_liquidity_snapshot(
        self, balances: dict[str, int], overview: dict[str, Any]
    ) -> tuple[float, float, float]:
        token0_meta = overview["token0"]
        token1_meta = overview["token1"]
        addr0 = token0_meta["address"]
        addr1 = token1_meta["address"]
        raw0 = int(balances.get(addr0, 0) or 0)
        raw1 = int(balances.get(addr1, 0) or 0)

        amount0 = from_erc20_raw(raw0, int(token0_meta["decimals"]))
        amount1 = from_erc20_raw(raw1, int(token1_meta["decimals"]))

        price0 = await self._token_price_usd(
            token0_meta.get("token_id") or USDC_TOKEN_ID
        )
        price1 = await self._token_price_usd(
            token1_meta.get("token_id") or THBILL_TOKEN_ID
        )
        total_value = amount0 * price0 + amount1 * price1
        return float(total_value), float(amount0), float(amount1)

    async def quote(self, deposit_amount: float | None = None) -> dict[str, Any]:
        ok, overview = await self.projectx.pool_overview()
        if not ok or not isinstance(overview, dict):
            return {
                "expected_apy": 0.0,
                "apy_type": "gross",
                "confidence": "low",
                "methodology": "Unable to fetch pool overview.",
                "components": {},
                "deposit_amount": deposit_amount,
                "as_of": datetime.now(UTC).isoformat(),
                "summary": f"Pool overview error: {overview}",
            }
        tick_spacing = int(overview["tick_spacing"])
        center_tick = await self._anchored_center_tick(
            int(overview["tick"]), tick_spacing
        )
        lower, upper = self._band_ticks(center_tick, tick_spacing)
        summary = f"Target band centered at tick {center_tick} spanning {upper - lower} ticks."
        out = {
            "expected_apy": 0.0,
            "apy_type": "gross",
            "confidence": "low",
            "methodology": "Band target derived from recent swap ticks (if available).",
            "components": {
                "tick": center_tick,
                "tick_lower": lower,
                "tick_upper": upper,
            },
            "deposit_amount": deposit_amount,
            "as_of": datetime.now(UTC).isoformat(),
            "summary": summary,
        }
        if deposit_amount is None or float(deposit_amount) <= 0:
            return out

        fee_tier = float(overview.get("fee") or 0)
        fee_rate = max(0.0, fee_tier / 1_000_000.0)
        if fee_rate <= 0:
            return out

        tick_now = int(overview.get("tick") or 0)
        out["components"]["tick_current"] = tick_now
        if tick_now < lower or tick_now >= upper:
            out["methodology"] = (
                "Band target derived from recent swap ticks (if available). "
                "Fee APY estimate omitted because the current tick is outside the proposed band."
            )
            return out

        sqrt_p = int(overview.get("sqrt_price_x96") or 0)
        if sqrt_p <= 0:
            return out

        token0_meta = overview.get("token0") or {}
        token1_meta = overview.get("token1") or {}
        dec0 = int(token0_meta.get("decimals", 18))
        dec1 = int(token1_meta.get("decimals", 18))
        price_token1_per_token0 = float(sqrt_price_x96_to_price(sqrt_p, dec0, dec1))
        if price_token1_per_token0 <= 0:
            return out

        sqrt_pl = sqrt_price_x96_from_tick(int(lower))
        sqrt_pu = sqrt_price_x96_from_tick(int(upper))

        # Estimate liquidity share from deposit size. This assumes the deposit is swapped into
        # an approximately optimal token0/token1 mix at the current mid-price.
        ref_liq = 2**128
        need0_ref, need1_ref = amounts_for_liq_inrange(
            sqrt_p, sqrt_pl, sqrt_pu, ref_liq
        )
        deposit_usd = float(deposit_amount)
        if deposit_usd <= 0:
            return out

        if need0_ref > 0 and need1_ref > 0:
            ratio_need0_over_need1 = float(need0_ref) / float(need1_ref)
            denom = (10**dec0) + (
                ratio_need0_over_need1 * price_token1_per_token0 * (10**dec1)
            )
            if denom <= 0:
                return out

            usdc_to_token0 = (
                ratio_need0_over_need1
                * price_token1_per_token0
                * (10**dec1)
                * deposit_usd
            ) / denom
            usdc_to_token0 = max(0.0, min(deposit_usd, usdc_to_token0))
            usdc_to_token1 = max(0.0, deposit_usd - usdc_to_token0)

            amount0_raw = int(usdc_to_token0 * (10**dec0))
            amount1_raw = int(usdc_to_token1 * price_token1_per_token0 * (10**dec1))
        elif need0_ref > 0:
            amount0_raw = int(deposit_usd * (10**dec0))
            amount1_raw = 0
        else:
            amount0_raw = 0
            amount1_raw = int(deposit_usd * price_token1_per_token0 * (10**dec1))

        liq_est = int(
            liq_for_amounts(sqrt_p, sqrt_pl, sqrt_pu, amount0_raw, amount1_raw)
        )
        if liq_est <= 0:
            return out

        pool_liquidity = int(overview.get("liquidity") or 0)
        if pool_liquidity > 0:
            liquidity_share_est = float(liq_est) / float(pool_liquidity + liq_est)
        else:
            liquidity_share_est = 1.0

        out["components"].update(
            {
                "fee_rate": fee_rate,
                "liquidity_pool_active": pool_liquidity,
                "liquidity_position_est": liq_est,
                "liquidity_share_est": liquidity_share_est,
            }
        )

        end_ts = int(time.time())
        start_ts = end_ts - int(self.QUOTE_FEE_WINDOW_SEC)
        swaps_ok, swaps = await self.projectx.fetch_swaps(
            limit=int(self.QUOTE_FEE_MAX_SWAPS),
            start_timestamp=start_ts,
            end_timestamp=end_ts,
        )
        if not swaps_ok or not isinstance(swaps, list):
            swaps = []

        volume_usd_total = 0.0
        volume_usd_in_range = 0.0
        swaps_with_usd = 0
        swaps_in_range = 0
        oldest_swap_ts: int | None = None

        for swap in swaps:
            try:
                ts = int(swap.get("timestamp") or 0)
            except Exception:
                ts = 0
            if ts:
                oldest_swap_ts = (
                    ts if oldest_swap_ts is None else min(oldest_swap_ts, ts)
                )

            tick_val = swap.get("tick")
            amount_usd_raw = swap.get("amount_usd")
            if tick_val is None or amount_usd_raw is None:
                continue
            try:
                tick_int = int(tick_val)
                amount_usd = abs(float(amount_usd_raw))
            except Exception:
                continue

            swaps_with_usd += 1
            volume_usd_total += amount_usd
            if lower <= tick_int < upper:
                swaps_in_range += 1
                volume_usd_in_range += amount_usd

        out["components"].update(
            {
                "window_sec": int(self.QUOTE_FEE_WINDOW_SEC),
                "swaps_with_usd": swaps_with_usd,
                "swaps_in_range": swaps_in_range,
                "volume_usd": volume_usd_total,
                "volume_usd_in_range": volume_usd_in_range,
            }
        )
        if oldest_swap_ts is not None:
            out["components"]["oldest_swap_ts"] = int(oldest_swap_ts)

        if swaps_with_usd <= 0 or volume_usd_total <= 0:
            out["methodology"] = (
                "Band target derived from recent swap ticks (if available). "
                "Fee APY estimate requires subgraph swap volume (amountUSD)."
            )
            return out

        fees_usd_window = volume_usd_in_range * fee_rate
        annualize = (365.0 * 24.0 * 60.0 * 60.0) / float(self.QUOTE_FEE_WINDOW_SEC)
        expected_fees_annual = fees_usd_window * annualize
        expected_fees_annual_to_you = expected_fees_annual * liquidity_share_est
        expected_apy = expected_fees_annual_to_you / deposit_usd

        confidence = "low"
        if oldest_swap_ts is not None and oldest_swap_ts <= start_ts:
            confidence = "medium"

        out["expected_apy"] = float(max(0.0, expected_apy))
        out["confidence"] = confidence
        out["methodology"] = (
            "Fee APY estimated from last-24h subgraph swap volume (amountUSD) that occurred while "
            "the pool tick was within the proposed band, multiplied by pool fee tier and an estimated "
            "share of active liquidity implied by the deposit size."
        )
        out["components"]["fees_usd_window"] = float(fees_usd_window)
        return out

    async def deposit(
        self, main_token_amount: float = 0.0, gas_token_amount: float = 0.0
    ) -> StatusTuple:
        if main_token_amount == 0.0 and gas_token_amount == 0.0:
            return (
                False,
                "Either main_token_amount or gas_token_amount must be provided",
            )

        if main_token_amount > 0 and main_token_amount < self.MINIMUM_NET_DEPOSIT:
            return (
                False,
                f"Minimum deposit is {self.MINIMUM_NET_DEPOSIT} USDC on HyperEVM.",
            )

        if gas_token_amount and gas_token_amount > self.GAS_MAXIMUM:
            return False, f"Gas token amount exceeds maximum: {self.GAS_MAXIMUM} HYPE"

        if not self.usdc_token_info:
            await self.setup()

        ok, main_usdc_raw = await self.balance_adapter.get_balance(
            token_id=USDC_TOKEN_ID, wallet_address=self._get_main_wallet_address()
        )
        if not ok or not isinstance(main_usdc_raw, int):
            return False, f"Failed to get main wallet USDC balance: {main_usdc_raw}"

        usdc_decimals = int(self.usdc_token_info.get("decimals", 6))
        available_usdc = from_erc20_raw(main_usdc_raw, usdc_decimals)
        if main_token_amount > 0:
            main_token_amount = min(float(main_token_amount), available_usdc)
            if main_token_amount < self.MINIMUM_NET_DEPOSIT:
                return (
                    False,
                    f"Insufficient USDC on HyperEVM main wallet: {available_usdc:.4f}",
                )

        ok, main_hype_raw = await self.balance_adapter.get_balance(
            token_id=GAS_TOKEN_ID, wallet_address=self._get_main_wallet_address()
        )
        if not ok or not isinstance(main_hype_raw, int):
            return False, f"Failed to get main wallet HYPE balance: {main_hype_raw}"

        ok, strat_hype_raw = await self.balance_adapter.get_balance(
            token_id=GAS_TOKEN_ID, wallet_address=self._get_strategy_wallet_address()
        )
        hype_decimals = int(self.hype_token_info.get("decimals", 18))
        strat_hype = (
            from_erc20_raw(strat_hype_raw, hype_decimals)
            if ok and isinstance(strat_hype_raw, int)
            else 0.0
        )
        main_hype = from_erc20_raw(main_hype_raw, hype_decimals)

        if gas_token_amount > 0:
            if main_hype < gas_token_amount:
                return (
                    False,
                    f"Main wallet HYPE balance {main_hype:.4f} < {gas_token_amount}",
                )
            (
                ok,
                msg,
            ) = await self.balance_adapter.move_from_main_wallet_to_strategy_wallet(
                GAS_TOKEN_ID, float(gas_token_amount), strategy_name=self.name
            )
            if not ok:
                return False, f"Failed to move HYPE to strategy wallet: {msg}"
            strat_hype += float(gas_token_amount)
        elif main_token_amount > 0 and strat_hype < self.GAS_THRESHOLD:
            top_up = min(self.GAS_THRESHOLD, self.GAS_MAXIMUM) - strat_hype
            top_up = max(0.0, top_up)
            if top_up > 0:
                if main_hype < top_up:
                    return (
                        False,
                        f"Main wallet HYPE balance {main_hype:.4f} < required gas top-up {top_up:.4f}",
                    )
                (
                    ok,
                    msg,
                ) = await self.balance_adapter.move_from_main_wallet_to_strategy_wallet(
                    GAS_TOKEN_ID, float(top_up), strategy_name=self.name
                )
                if not ok:
                    return False, f"Failed to top up HYPE gas: {msg}"

        if main_token_amount > 0:
            (
                ok,
                msg,
            ) = await self.balance_adapter.move_from_main_wallet_to_strategy_wallet(
                USDC_TOKEN_ID, float(main_token_amount), strategy_name=self.name
            )
            if not ok:
                return False, f"Failed to move USDC to strategy wallet: {msg}"

        overview_ok, overview = await self.projectx.pool_overview()
        if not overview_ok or not isinstance(overview, dict):
            return False, f"Failed to fetch ProjectX pool overview: {overview}"

        positions_ok, positions = await self.projectx.list_positions()
        if not positions_ok or not isinstance(positions, list):
            return False, f"Failed to list ProjectX positions: {positions}"
        slippage_bps = max(
            5, int(float(THBILL_USDC_METADATA.get("band_bps", 20)) // 2) or 10
        )

        if positions:
            pos = positions[0]
            inc_ok, inc = await self.projectx.increase_liquidity_balanced(
                pos.token_id,
                pos.tick_lower,
                pos.tick_upper,
                slippage_bps=slippage_bps,
            )
            if not inc_ok or not isinstance(inc, dict):
                return False, f"Failed to increase liquidity: {inc}"
            tx_hash = inc.get("tx_hash")
            spend_meta = inc.get("spent") if isinstance(inc.get("spent"), dict) else {}
            message = f"Added {main_token_amount:.2f} USDC into existing ProjectX position {pos.token_id}."
            spend_note = self._format_spent_summary(
                spend_meta, overview["token0"], overview["token1"]
            )
            if spend_note:
                message += f" {spend_note}."
            if tx_hash:
                message += f" Tx: {self._tx_link(str(tx_hash))}"
            return True, message

        center_tick = await self._anchored_center_tick(
            int(overview["tick"]), int(overview["tick_spacing"])
        )
        tick_lower, tick_upper = self._band_ticks(
            center_tick, int(overview["tick_spacing"])
        )
        mint_ok, mint = await self.projectx.mint_from_balances(
            tick_lower,
            tick_upper,
            slippage_bps=slippage_bps,
        )
        if not mint_ok or not isinstance(mint, dict):
            return False, f"Mint failed: {mint}"
        token_id = mint.get("token_id")
        tx_hash = mint.get("tx_hash")
        spend_meta = mint.get("spent") if isinstance(mint.get("spent"), dict) else {}
        if not token_id:
            return False, "Mint failed; unable to detect new liquidity position."

        notes = [f"Opened ProjectX position #{token_id} @ [{tick_lower}, {tick_upper}]"]
        spend_note = self._format_spent_summary(
            spend_meta, overview["token0"], overview["token1"]
        )
        if spend_note:
            notes.append(f"{spend_note}.")
        if tx_hash:
            notes.append(f"Mint tx: {self._tx_link(str(tx_hash))}")
        return True, "\n".join(notes)

    async def update(self) -> StatusTuple:
        overview_ok, overview = await self.projectx.pool_overview()
        if not overview_ok or not isinstance(overview, dict):
            return False, f"Failed to fetch ProjectX pool overview: {overview}"
        tick = int(overview["tick"])
        spacing = int(overview["tick_spacing"])
        slippage_bps = max(
            5, int(float(THBILL_USDC_METADATA.get("band_bps", 20)) // 2) or 10
        )

        positions_ok, positions = await self.projectx.list_positions()
        if not positions_ok or not isinstance(positions, list):
            return False, f"Failed to list ProjectX positions: {positions}"
        if not positions:
            balances_ok, balances = await self.projectx.current_balances()
            if not balances_ok or not isinstance(balances, dict):
                return False, f"Failed to fetch ProjectX balances: {balances}"
            idle_value, *_ = await self._idle_liquidity_snapshot(balances, overview)
            if idle_value < IDLE_REDEPLOY_THRESHOLD_USD:
                return False, "No active ProjectX positions; deposit first."
            center_tick = await self._anchored_center_tick(tick, spacing)
            tick_lower, tick_upper = self._band_ticks(center_tick, spacing)
            mint_ok, mint = await self.projectx.mint_from_balances(
                tick_lower, tick_upper, slippage_bps=slippage_bps
            )
            if not mint_ok or not isinstance(mint, dict):
                return False, f"Mint failed: {mint}"
            token_id = mint.get("token_id")
            tx_hash = mint.get("tx_hash")
            spend_meta = (
                mint.get("spent") if isinstance(mint.get("spent"), dict) else {}
            )
            if not token_id:
                return False, "Unable to deploy idle balances into ProjectX pool."
            note = f"Initialized ProjectX position #{token_id} using idle balances."
            spend_note = self._format_spent_summary(
                spend_meta, overview["token0"], overview["token1"]
            )
            if spend_note:
                note += f" {spend_note}."
            if tx_hash:
                note += f" Tx: {self._tx_link(str(tx_hash))}"
            return True, note

        pos = positions[0]
        position_value_usd = await self._position_value_usd(pos, overview)
        out_of_band = await self._should_recentre(
            pos, spacing=spacing, current_tick=tick
        )

        if out_of_band:
            burn_ok, burn_tx = await self.projectx.burn_position(pos.token_id)
            if not burn_ok:
                return False, f"Failed to burn position {pos.token_id}: {burn_tx}"

            center_tick = await self._anchored_center_tick(tick, spacing)
            tick_lower, tick_upper = self._band_ticks(center_tick, spacing)
            mint_ok, mint = await self.projectx.mint_from_balances(
                tick_lower, tick_upper, slippage_bps=slippage_bps
            )
            if not mint_ok or not isinstance(mint, dict):
                return False, f"Mint failed: {mint}"
            token_id = mint.get("token_id")
            tx_hash = mint.get("tx_hash")
            spend_meta = (
                mint.get("spent") if isinstance(mint.get("spent"), dict) else {}
            )
            message = f"Recentred liquidity into ticks {tick_lower}-{tick_upper} (token {token_id})."
            spend_note = self._format_spent_summary(
                spend_meta, overview["token0"], overview["token1"]
            )
            if spend_note:
                message += f" {spend_note}."
            if tx_hash:
                message += f" Tx: {self._tx_link(str(tx_hash))}"
            return True, message

        # Collect fees if meaningful (best-effort)
        try:
            fee_ok, fee_snapshot = await self.projectx.live_fee_snapshot(pos.token_id)
            if not fee_ok or not isinstance(fee_snapshot, dict):
                raise RuntimeError(str(fee_snapshot))
            total_usd = float(fee_snapshot.get("usd") or 0.0)
            threshold = max(
                MIN_AUTOCOLLECT_USD,
                float(position_value_usd) * (MIN_AUTOCOLLECT_PCT / 100),
            )
            if total_usd >= threshold:
                await self.projectx.collect_fees(pos.token_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Fee snapshot/collect failed: {exc}")

        balances_ok, balances = await self.projectx.current_balances()
        if not balances_ok or not isinstance(balances, dict):
            return False, f"Failed to fetch ProjectX balances: {balances}"
        idle_value, *_ = await self._idle_liquidity_snapshot(balances, overview)
        redeploy_threshold = max(
            IDLE_REDEPLOY_THRESHOLD_USD,
            float(position_value_usd) * (MIN_AUTOCOLLECT_PCT / 100),
        )
        if idle_value >= redeploy_threshold:
            inc_ok, inc = await self.projectx.increase_liquidity_balanced(
                pos.token_id,
                pos.tick_lower,
                pos.tick_upper,
                slippage_bps=slippage_bps,
            )
            if not inc_ok or not isinstance(inc, dict):
                return False, f"Failed to increase liquidity: {inc}"
            tx_hash = inc.get("tx_hash")
            spend_meta = inc.get("spent") if isinstance(inc.get("spent"), dict) else {}
            message = "Compounded fees and redeployed idle balances."
            spend_note = self._format_spent_summary(
                spend_meta, overview["token0"], overview["token1"]
            )
            if spend_note:
                message += f" {spend_note}."
            if tx_hash:
                message += f" Tx: {self._tx_link(str(tx_hash))}"
            return True, message

        return True, "Compounded fees; no idle balances to deploy."

    async def withdraw(self, **kwargs: Any) -> StatusTuple:
        positions_ok, positions = await self.projectx.list_positions()
        if not positions_ok or not isinstance(positions, list):
            return False, f"Failed to list ProjectX positions: {positions}"
        for pos in positions:
            burn_ok, burn_tx = await self.projectx.burn_position(pos.token_id)
            if not burn_ok:
                return False, f"Failed to burn position {pos.token_id}: {burn_tx}"

        overview_ok, overview = await self.projectx.pool_overview()
        if not overview_ok or not isinstance(overview, dict):
            return False, f"Failed to fetch ProjectX pool overview: {overview}"

        balances_ok, balances = await self.projectx.current_balances()
        if not balances_ok or not isinstance(balances, dict):
            return False, f"Failed to fetch ProjectX balances: {balances}"
        token0_addr = str(overview["token0"]["address"])
        token1_addr = str(overview["token1"]["address"])

        token1_balance = int(balances.get(token1_addr, 0) or 0)
        if token1_balance > 0:
            swap_ok, swap_tx = await self.projectx.swap_exact_in(
                token1_addr,
                token0_addr,
                token1_balance,
                slippage_bps=40,
            )
            if not swap_ok:
                return False, f"Swap failed: {swap_tx}"

        return (
            True,
            "Closed all ProjectX positions and converted to USDC. Funds remain in strategy wallet.",
        )

    async def exit(self, **kwargs: Any) -> StatusTuple:
        ok, strat_usdc_raw = await self.balance_adapter.get_balance(
            token_id=USDC_TOKEN_ID, wallet_address=self._get_strategy_wallet_address()
        )
        if not ok or not isinstance(strat_usdc_raw, int) or strat_usdc_raw <= 0:
            return False, "No USDC in strategy wallet to transfer."
        usdc_decimals = int(self.usdc_token_info.get("decimals", 6))
        usdc_tokens = from_erc20_raw(strat_usdc_raw, usdc_decimals)
        ok, msg = await self.balance_adapter.move_from_strategy_wallet_to_main_wallet(
            USDC_TOKEN_ID, usdc_tokens, strategy_name=self.name
        )
        if not ok:
            return False, f"Failed to transfer USDC to main wallet: {msg}"
        return (
            True,
            f"Transferred {usdc_tokens:.4f} USDC from strategy wallet to main wallet.",
        )

    async def _status(self) -> StatusDict:
        positions_ok, positions = await self.projectx.list_positions()
        overview_ok, overview = await self.projectx.pool_overview()

        ok, gas_raw = await self.balance_adapter.get_balance(
            token_id=GAS_TOKEN_ID, wallet_address=self._get_strategy_wallet_address()
        )
        gas_amount = (
            from_erc20_raw(gas_raw, int(self.hype_token_info.get("decimals", 18)))
            if ok and isinstance(gas_raw, int)
            else 0.0
        )

        _, net_deposit = await self.ledger_adapter.get_strategy_net_deposit(
            wallet_address=self._get_strategy_wallet_address()
        )

        points_ok, prjx_points = await self.projectx.fetch_prjx_points(
            self._get_strategy_wallet_address()
        )
        if not points_ok or not isinstance(prjx_points, dict):
            prjx_points = {"error": str(prjx_points)}

        if not positions_ok or not isinstance(positions, list):
            positions = []
        if not overview_ok or not isinstance(overview, dict):
            status_err = f"ProjectX status unavailable (overview: {overview})"
            return StatusDict(
                portfolio_value=0.0,
                net_deposit=float(net_deposit or 0.0),
                strategy_status=status_err,
                gas_available=float(gas_amount),
                gassed_up=float(gas_amount) >= float(self.GAS_THRESHOLD),
                projectx_points=prjx_points,
                fees_live_usd=0.0,
            )

        sqrt_p = int(overview["sqrt_price_x96"])
        token0_meta = overview["token0"]
        token1_meta = overview["token1"]

        price_token0 = await self._token_price_usd(
            token0_meta.get("token_id") or USDC_TOKEN_ID
        )
        price_token1 = await self._token_price_usd(
            token1_meta.get("token_id") or THBILL_TOKEN_ID
        )

        portfolio_value = 0.0
        liquidity_summary: list[str] = []
        total_live_fees = 0.0

        swaps_ok, recent_swaps = await self.projectx.fetch_swaps(limit=25)
        if not swaps_ok or not isinstance(recent_swaps, list):
            recent_swaps = []
        recent_ticks = [
            int(s.get("tick", 0)) for s in recent_swaps if s.get("tick") is not None
        ]
        tick_now = int(overview.get("tick") or 0)
        ticks_for_state = recent_ticks or [tick_now]
        tick_sample_count = len(recent_ticks)
        swap_window_desc = (
            f"last {tick_sample_count} swaps" if tick_sample_count else "latest tick"
        )

        for pos in positions:
            sqrt_pl = sqrt_price_x96_from_tick(int(pos.tick_lower))
            sqrt_pu = sqrt_price_x96_from_tick(int(pos.tick_upper))
            amount0, amount1 = amounts_for_liq_inrange(
                sqrt_p, sqrt_pl, sqrt_pu, int(pos.liquidity)
            )
            amount0_tokens = from_erc20_raw(amount0, int(token0_meta["decimals"]))
            amount1_tokens = from_erc20_raw(amount1, int(token1_meta["decimals"]))
            contribution = amount0_tokens * price_token0 + amount1_tokens * price_token1

            fee_usd = 0.0
            fee_ok, fee_snapshot = await self.projectx.live_fee_snapshot(pos.token_id)
            if fee_ok and isinstance(fee_snapshot, dict):
                fee_usd = float(fee_snapshot.get("usd") or 0.0)
                total_live_fees += fee_usd

            position_contribution = contribution + fee_usd
            portfolio_value += position_contribution

            range_state = self.projectx.classify_range_state(
                ticks_for_state, pos.tick_lower, pos.tick_upper, fallback_tick=tick_now
            )
            state_label = {
                "in_range": f"In range ({swap_window_desc})",
                "entering_out_of_range": f"Entering out of range ({swap_window_desc})",
                "out_of_range": f"Out of range ({swap_window_desc})",
                "unknown": "Range unknown",
            }.get(range_state, "Range unknown")

            liquidity_summary.append(
                f"NFT {pos.token_id}: [{pos.tick_lower},{pos.tick_upper}] "
                f"≈ {position_contribution:.2f} USDC | State: {state_label} | Owed≈${fee_usd:.4f}"
            )

        balances_ok, balances = await self.projectx.current_balances()
        if balances_ok and isinstance(balances, dict):
            idle_value, *_ = await self._idle_liquidity_snapshot(balances, overview)
        else:
            idle_value = 0.0
        portfolio_value += float(idle_value)

        return StatusDict(
            portfolio_value=float(portfolio_value),
            net_deposit=float(net_deposit or 0.0),
            strategy_status="\n".join(liquidity_summary) or "No active NFT positions",
            gas_available=float(gas_amount),
            gassed_up=float(gas_amount) >= float(self.GAS_THRESHOLD),
            projectx_points=prjx_points,
            fees_live_usd=float(total_live_fees),
        )

    @staticmethod
    async def policies() -> list[str]:
        router = str(THBILL_USDC_METADATA["router"])
        npm = str(THBILL_USDC_METADATA["npm"])
        return [
            erc20_spender_for_any_token(router),
            await prjx_swap(),
            erc20_spender_for_any_token(npm),
            await prjx_npm(),
        ]
