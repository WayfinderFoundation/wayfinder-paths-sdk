"""Selectable, paper-first jobs_v1 starter strategies.

The catalog owns exact rules and research provenance. Selecting a starter
creates a normal Wayfinder job; from that point, the standard backtest and
forward-result machinery owns the user's results.
"""

from __future__ import annotations

import copy
import importlib
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.background import spawn_detached_op
from wayfinder_paths.jobs.models import (
    AgentMode,
    WayfinderJob,
    normalize_agent_mode,
    safe_job_id,
)
from wayfinder_paths.jobs.starter_leverage_evidence import STARTER_LEVERAGE_RESULTS
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.strategies._starter_utils import (
    MEAN_REVERSION_STOP_DEFAULTS,
    PAIR_PROTECTION_DEFAULTS,
    RANKING_STOP_DEFAULTS,
)

STARTER_CATALOG_VERSION = "2.0.0"
STARTER_STRATEGY_INCEPTION_AT = "2026-08-24T00:00:00+00:00"
# Catalog launch policy: every off-the-shelf starter launches with the agent
# loop ON in intervene mode. Fleet evidence (two launches of the identical
# starter): the intervene copy was the only productive research job (4
# experiments/72h); the monitor twin burned ~49 wakes/48h unable to act — it
# holdout-CONFIRMED a hypothesis and could not open its pre-registered paper
# probation leg; agent-off copies did zero research. Callers may override
# deliberately, but the default is intervene.
STARTER_AGENT_MODE_DEFAULT: AgentMode = "intervene"
STARTER_AGENT_WAKE_SECONDS = 3600
STARTER_EVIDENCE_REVISION = "1.8.0"
STARTER_LEVERAGE_DEFAULT = 1
STARTER_LEVERAGE_MINIMUM = 1
STARTER_LEVERAGE_MAXIMUM = 5
STARTER_LEVERAGE_STEP = 1
# Evidence-window owner policy: starter backtests/validation replay 120 days.
STARTER_DATASET_DAYS = 120
# Slack on top of the strategy's warmup gate so the live driver's sliding
# window always clears warmup even when the feed drops a few leading bars.
STARTER_LOOKBACK_MARGIN_BARS = 20
STARTER_ROBUSTNESS_PLANS: dict[str, dict[str, Any]] = {
    "crypto-momentum-persistence-4h": {
        "neighbors": {"broad_bull_momentum_threshold": [0.05, 0.10, 0.15]},
        "phase": {"param": "rebalance_offset", "values": [0, 1, 2, 3, 4, 5]},
        "leverage": [1, 2, 3, 4, 5],
        "walk_forward": {"train_bars": 1440, "test_bars": 360, "folds": 4},
        "scenarios": [{"name": "recent_7d", "lookback_days": 7, "role": "development"}],
    }
}


def validate_starter_leverage(value: Any) -> int:
    """Validate the starter selector's intentionally narrow leverage range."""
    if isinstance(value, bool):
        raise ValueError("starter leverage must be a whole number from 1 to 5")
    try:
        candidate = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("starter leverage must be a whole number from 1 to 5") from exc
    if (
        not math.isfinite(candidate)
        or not candidate.is_integer()
        or not STARTER_LEVERAGE_MINIMUM <= candidate <= STARTER_LEVERAGE_MAXIMUM
    ):
        raise ValueError("starter leverage must be a whole number from 1 to 5")
    return int(candidate)


def coerce_starter_leverage(value: Any) -> tuple[int, str | None]:
    """Reuse-path tolerant variant of validate_starter_leverage.

    An existing job whose recorded leverage drifted outside the starter dial
    (hand edit, governance clamp) must never brick reopen: out-of-range or
    invalid values clamp to the nearest valid whole number with a warning
    instead of raising. New selections still go through the strict path."""
    try:
        return validate_starter_leverage(value), None
    except ValueError:
        pass
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = math.nan
    if not math.isfinite(numeric):
        return STARTER_LEVERAGE_DEFAULT, (
            f"existing leverage {value!r} is not usable; using the starter "
            f"default {STARTER_LEVERAGE_DEFAULT}"
        )
    clamped = int(
        min(max(round(numeric), STARTER_LEVERAGE_MINIMUM), STARTER_LEVERAGE_MAXIMUM)
    )
    return clamped, (
        f"existing leverage {value!r} is outside the starter dial "
        f"({STARTER_LEVERAGE_MINIMUM}-{STARTER_LEVERAGE_MAXIMUM}); "
        f"clamped to {clamped}"
    )


@dataclass(frozen=True)
class StarterDefinition:
    id: str
    name: str
    family: str
    summary: str
    timeframe: str
    module: str
    symbols: tuple[str, ...]
    crypto_assets: tuple[str, ...]
    tokenized_equities: tuple[str, ...]
    rules: tuple[str, ...]
    params: dict[str, Any]
    research_evidence: dict[str, Any]
    strategy_inception_at: str = STARTER_STRATEGY_INCEPTION_AT
    cautions: tuple[str, ...] = ()
    # Declared feature feeds (data_contract.features): the wake refresh keeps
    # each one live and the strategy stands down while a feed is stale.
    features: tuple[dict[str, Any], ...] = ()

    def configured_params(self) -> dict[str, Any]:
        if self.family in {"mean_reversion", "maker_mean_reversion"}:
            protection = MEAN_REVERSION_STOP_DEFAULTS
        elif self.family == "relative_value_pair":
            protection = PAIR_PROTECTION_DEFAULTS
        else:
            protection = {
                **RANKING_STOP_DEFAULTS,
                "stop_atr_period": 96 if self.timeframe == "15m" else 24,
            }
        return {**copy.deepcopy(protection), **copy.deepcopy(self.params)}

    def risk_limits(self) -> dict[str, Any]:
        max_drawdown = (
            -0.06
            if self.family == "mean_reversion"
            else -0.08
            if self.family == "maker_mean_reversion"
            else -0.20
        )
        return {
            "max_drawdown": max_drawdown,
            "pause_after_consecutive_losses": 5,
        }

    def risk_controls(self) -> dict[str, Any]:
        params = self.configured_params()
        controls: dict[str, Any] = {
            "per_position_stop": {
                "basis": f"{params['stop_atr_multiple']:g}x ATR({params['stop_atr_period']})",
                "minimum_pct": params["stop_min_pct"],
                "maximum_pct": params["stop_max_pct"],
                "native_when_live": params["native_stop_required"],
                "take_profit": None,
            },
            "account_halt": {
                **self.risk_limits(),
                "flatten_on_breach": False,
                "manual_resume_required": True,
            },
        }
        if params.get("stop_cooldown_seconds"):
            controls["per_position_stop"]["cooldown_seconds"] = params[
                "stop_cooldown_seconds"
            ]
        if self.family == "maker_mean_reversion":
            if params.get("exit_mode") in {"full", "staged"}:
                controls["per_position_stop"]["take_profit"] = (
                    (
                        f"sell {params['take_profit_one_fraction'] * 100:g}% at "
                        f"{params['take_profit_one_atr']:g}x entry ATR, then the "
                        f"remainder at {params['take_profit_two_atr']:g}x"
                    )
                    if params["exit_mode"] == "staged"
                    else f"full sell at {params['take_profit_atr']:g}x entry ATR"
                )
            else:
                controls["per_position_stop"]["take_profit"] = (
                    f"taker exit above RSI {params['exit_rsi']:g} or after "
                    f"{params['max_hold_bars']} completed bars"
                )
        if self.family == "relative_value_pair":
            controls["pair_group_stop"] = {
                "monitor_interval_seconds": params[
                    "protection_monitor_interval_seconds"
                ],
                "loss_budget": (
                    f"minimum of {params['pair_max_entry_equity_loss_pct'] * 100:g}% "
                    "of entry account equity and "
                    f"{params['pair_max_entry_gross_loss_pct'] * 100:g}% of entry "
                    "gross notional"
                ),
                "close_companion_on_leg_stop": True,
                "cross_symbol_atomic": False,
                "halt_after_exit": True,
            }
        return controls

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["params"] = {
            **self.configured_params(),
            "lookback_bars": starter_lookback_bars(self),
        }
        payload["risk_limits"] = self.risk_limits()
        payload["risk_controls"] = self.risk_controls()
        payload["leverage_control"] = {
            "minimum": STARTER_LEVERAGE_MINIMUM,
            "maximum": STARTER_LEVERAGE_MAXIMUM,
            "step": STARTER_LEVERAGE_STEP,
            "default": STARTER_LEVERAGE_DEFAULT,
            "operator_owned": True,
        }
        payload["research_evidence"] = {
            **payload["research_evidence"],
            "strategy_revision": payload["research_evidence"].get(
                "strategy_revision", STARTER_EVIDENCE_REVISION
            ),
            "risk_overlay_backtest_status": "validated",
            "risk_overlay_backtest_scope": "per_position_ohlc_stops",
            "risk_overlay_note": (
                (
                    "The jobs_v1 figures include strict candle trade-through maker "
                    "fills and the per-position OHLC stop. Live limit routing stays "
                    "disabled until durable venue fill/cancel reconciliation lands."
                )
                if self.family == "maker_mean_reversion"
                else (
                    "The jobs_v1 engine figures include the current per-position "
                    "stop overlay. Live pair-group and account monitors run between "
                    "strategy bars and are not included in these historical figures."
                    if self.family == "relative_value_pair"
                    else "The jobs_v1 engine figures include the current per-position "
                    "stop overlay. The live account monitor runs between strategy "
                    "bars and is not included in these historical figures."
                )
            ),
            "jobs_v1_leverage_sweep": {
                "leverage_semantics": "target_exposure",
                "liquidation_model": (
                    "close-of-bar cross margin using venue maintenance defaults"
                ),
                "account_halt_simulated": False,
                "results": copy.deepcopy(STARTER_LEVERAGE_RESULTS[self.id]),
            },
        }
        if self.id in STARTER_ROBUSTNESS_PLANS:
            payload["robustness_plan"] = copy.deepcopy(
                STARTER_ROBUSTNESS_PLANS[self.id]
            )
        for key in (
            "symbols",
            "crypto_assets",
            "tokenized_equities",
            "rules",
            "cautions",
        ):
            payload[key] = list(payload[key])
        payload.update(
            {
                "catalog_version": STARTER_CATALOG_VERSION,
                "strategy_inception_at": self.strategy_inception_at,
                "execution_contract": "jobs_v1",
                "default_mode": "paper",
                "selectable": True,
                "forward_tracking": {
                    "starts": "when_selected",
                    "initial_status": "no_forward_observations",
                },
                "risk_notice": (
                    "Positive historical expectancy is not a guarantee. "
                    "Start in paper mode and evaluate forward results from "
                    "the job's own inception before considering live risk."
                ),
                "wallet_ownership_notice": (
                    "Native protection reconciles only this job's exact client "
                    "order ids. Other orders on a shared wallet are never "
                    "canceled, but shared-wallet exposure can still affect "
                    "account-level limits. A dedicated wallet is recommended."
                ),
            }
        )
        return payload


def starter_warmup_bars(definition: StarterDefinition) -> int:
    """The strategy's own warmup gate, computed from the exact params the
    launched job will run with. Every starter strategy declares
    ``warmup_bars`` in ``__init__``; a module that doesn't fails loudly here
    (and in the catalog test) instead of shipping a starter that never trades.
    """
    module = importlib.import_module(definition.module)
    strategy = module.build_strategy(
        {**definition.configured_params(), "symbols": list(definition.symbols)}
    )
    warmup = int(strategy.warmup_bars)
    if warmup <= 0:
        raise ValueError(f"starter {definition.id}: warmup_bars must be positive")
    return warmup


def starter_lookback_bars(definition: StarterDefinition) -> int:
    """Live-driver window for this starter: strategy warmup plus margin.

    The live/paper driver hands strategies a sliding window of
    ``lookback_bars`` completed bars (default 200), so ``ctx.bar_index`` is
    capped at the window length. A window smaller than the strategy's warmup
    gate means the starter NEVER trades. Deriving the window from the
    strategy's declared warmup keeps catalog edits from reintroducing the
    mismatch.
    """
    return starter_warmup_bars(definition) + STARTER_LOOKBACK_MARGIN_BARS


_RESEARCH_METHOD = {
    "source": "Hydromancer Reservoir 1-second Hyperliquid/HIP-3 candles",
    "window_end": "2026-08-16T23:45:00+00:00",
    "fill_model": "decision on completed close; fill at next bar open",
    "costs": {"taker_fee_bps_per_side": 4.5, "slippage_bps_per_side": 3.5},
    "funding": "Hyperliquid hourly historical funding applied by signed exposure",
    "validation": (
        "four chronological folds; daily rank strategies also checked at "
        "neighboring UTC rebalance phases"
    ),
}

_CRYPTO_MOMENTUM_RESEARCH_METHOD = {
    "source": "Hyperliquid info API 4h candles via HyperliquidDataClient",
    "window_start": "2024-09-04T00:00:00+00:00",
    "window_end": "2026-08-17T16:00:00+00:00",
    "calendar_days": 712.7,
    "fill_model": "decision on completed close; fill at next bar open",
    "costs": {"taker_fee_bps_per_side": 4.5, "slippage_bps_per_side": 3.5},
    "funding_included": False,
    "funding": "not included; long and short carry can change live returns",
    "validation": (
        "training-only rank admission, four rolling 240-day train / 60-day "
        "test folds, neighboring broad-bull thresholds, and all six daily "
        "4h rebalance phases"
    ),
}

_PAIR_RESEARCH_METHOD = {
    "source": "Hyperliquid info API daily candles",
    "window_start": "2024-08-17T00:00:00+00:00",
    "window_end": "2026-08-17T00:00:00+00:00",
    "calendar_days": 730.0,
    "fill_model": "decision on completed close; fill at next bar open",
    "costs": {"taker_fee_bps_per_side": 4.5, "slippage_bps_per_side": 3.5},
    "funding": (
        "Binance USD-M historical funding used as a two-year carry proxy; "
        "the jobs_v1 replay excludes funding until the simulator consumes "
        "funding feature rows as settlement events"
    ),
    "validation": (
        "four chronological folds; conventional Monday rebalance plus all "
        "seven neighboring weekly phases checked"
    ),
}

_MAKER_RESEARCH_METHOD = {
    "source": "Hydromancer Reservoir 1-second Hyperliquid HYPE candles",
    "window_start": "2025-08-01T00:00:00+00:00",
    "window_end": "2026-08-18T23:55:00+00:00",
    "calendar_days": 383.0,
    "fill_model": (
        "decision on completed 5m close; post-only order first eligible on the "
        "next bar; require 1bp trade-through beyond the limit"
    ),
    "costs": {
        "maker_fee_bps_per_side": 1.5,
        "taker_fee_bps_per_side": 4.5,
        "taker_slippage_bps_per_side": 3.5,
    },
    "funding_included": False,
    "funding": "not included; exposure is sparse but carry remains a live risk",
    "validation": (
        "pooled multiple-testing correction across nine assets and 5m/15m bars; "
        "reserved 15% tail; four rolling 187.5-day train / 46.9-day test folds"
    ),
}

_DIVERSE_INTRADAY_RESEARCH_METHOD = {
    "source": "Hydromancer Reservoir 1-second Hyperliquid candles aggregated to 15m",
    "window_start": "2025-10-02T13:30:00+00:00",
    "window_end": "2026-08-25T00:00:00+00:00",
    "calendar_days": 326.4,
    "fill_model": "decision on completed close; fill at next bar open",
    "costs": {"taker_fee_bps_per_side": 4.5, "slippage_bps_per_side": 3.5},
    "funding_included": False,
    "funding": "not included; long and short carry can change live returns",
    "validation": (
        "parameter grids ranked on the first 60% of common asset history; the "
        "next 20%, final 20%, jobs_v1 replay, and every UTC rebalance phase were "
        "reported separately but reviewed before publication; no sealed holdout"
    ),
}

_BULLISH_5M_RESEARCH_METHOD = {
    "source": "Binance USD-M native 5m candles via the SDK CCXT dataset fetcher",
    "window_start": "2025-09-04T15:25:00+00:00",
    "window_end": "2026-09-04T15:15:00+00:00",
    "calendar_days": 365.0,
    "fill_model": "decision on completed 5m close; fill at next 5m bar open",
    "costs": {"taker_fee_bps_per_side": 4.5, "slippage_bps_per_side": 3.5},
    "funding_included": False,
    "funding": "not included; long carry can change live returns",
    "validation": (
        "native-resolution cross-venue replay of a mechanism developed on an "
        "independent Hyperliquid 15m panel; all slices were reviewed before "
        "publication, so forward paper results remain the real holdout"
    ),
}

_FUNDING_OI_DIVERGENCE_RESEARCH_METHOD = {
    "source": (
        "Hydromancer Reservoir 1-second Hyperliquid candles aggregated to 15m, "
        "hourly Hyperliquid funding through the SDK funding fetcher, and daily "
        "open interest aggregated from a Hyperliquid account-snapshot archive"
    ),
    "window_start": "2025-07-31T00:15:00+00:00",
    "window_end": "2026-09-04T00:00:00+00:00",
    "calendar_days": 400.0,
    "tradeable_from": "2025-08-30T00:00:00+00:00",
    "fill_model": (
        "decision on completed close; market fills at next bar open; post-only "
        "fills require a 1 bp candle trade-through of the resting price"
    ),
    "costs": {
        "taker_fee_bps_per_side": 4.5,
        "slippage_bps_per_side": 3.5,
        "maker_fee_bps_per_side": 1.5,
    },
    "funding_included": False,
    "funding": (
        "not included; the fade is paid funding on average, see funding_pnl_note"
    ),
    "validation": (
        "indicator screened on 19 Hyperliquid perps against a Binance same-year "
        "and four-year replay, then every candidate was re-simulated per symbol "
        "in the jobs_v1 engine and pooled (cross_asset_lift); the plain "
        "funding-divergence and open-interest-unwind variants failed that bar; "
        "all slices were reviewed before publication, so forward paper results "
        "remain the real holdout"
    ),
}

_FUNDING_OI_DIVERGENCE_SYMBOLS = (
    "BTC",
    "ETH",
    "HYPE",
    "SOL",
    "XRP",
    "NEAR",
    "PUMP",
    "WLD",
    "SUI",
    "DOGE",
    "TRUMP",
    "FARTCOIN",
    "AAVE",
    "VVV",
    "ENA",
    "TAO",
    "ONDO",
    "BNB",
    "kPEPE",
)

_FUNDING_OI_DIVERGENCE_FEATURES = (
    {"name": "funding", "max_age_seconds": 7200, "stale_policy": "skip"},
    {"name": "open_interest", "max_age_seconds": 172_800, "stale_policy": "skip"},
)

_FUNDING_OI_DIVERGENCE_PARAMS: dict[str, Any] = {
    "funding_z_window_bars": 2880,
    "funding_z_entry": 2.0,
    "confirm_return_bars": 96,
    "confirm_return_max": 0.0,
    "oi_confirmation": "building",
    "oi_lookback_bars": 96,
    "max_hold_bars": 96,
    "weight_per_leg": 0.05,
    "maker_fee_bps": 1.5,
    "maker_trade_through_bps": 1.0,
    # catastrophe stop only: the mean-reversion overlay bound the 24-hour hold
    # in three of four quarters (37 stops, +3.6% against +6.3% without), and
    # the ranking floor of 25% fired once on a 27% VVV squeeze (-8 bps)
    "stop_atr_period": 24,
    "stop_atr_multiple": 12.0,
    "stop_min_pct": 0.30,
    "stop_max_pct": 0.50,
}

_FUNDING_OI_DIVERGENCE_CAUTIONS = (
    "Open interest has no public history on Hyperliquid: a new job records it at every wake from its first day, and the strategy stands down until a full day of open-interest history exists. The backtest used a daily archive of account snapshots.",
    "Hourly funding is required; the edge disappeared when the same rules ran on 8-hour averaged funding.",
    "The funding-only signal was regime dependent over four Binance years (negative in 2022 and 2024); the open-interest-confirmed book has one year of Hyperliquid history and eight of nineteen symbols carried the taker book.",
    "This fades a crowded side: liquidation cascades can move further than the 24-hour hold's usual range, and the catastrophe stop is the only per-position guard. Its 30% floor was chosen after the 25% ranking floor fired once on a 27% VVV squeeze; a 5% leg can still lose 1.5–2.5% of equity before it triggers.",
    "Funding P&L is excluded from the headline figures.",
)


STARTER_DEFINITIONS: tuple[StarterDefinition, ...] = (
    StarterDefinition(
        id="bullish-regime-rotation-5m",
        name="Bullish Regime Rotation · 5m",
        family="regime_rotation",
        summary=(
            "Owns one confirmed medium-term leader during broad uptrends and "
            "otherwise holds cash, using a deliberately modest 40% gross allocation."
        ),
        timeframe="5m",
        module="wayfinder_paths.jobs.strategies.regime_rotation",
        symbols=("BNB", "PAXG", "HYPE", "ZEC", "MORPHO"),
        crypto_assets=("BNB", "PAXG", "HYPE", "ZEC", "MORPHO"),
        tokenized_equities=(),
        rules=(
            "Treat an asset as bullish only when its 24-hour average and price are above its 5-day average and its trailing 3-day return is positive.",
            "Hold the strongest 3-day leader only when at least three of five assets are bullish; otherwise hold cash.",
            "Re-evaluate daily at 12:00 UTC and cap target gross exposure at 40%.",
        ),
        params={
            "risk_symbols": ["BNB", "PAXG", "HYPE", "ZEC", "MORPHO"],
            "defensive_symbol": None,
            "momentum_bars": 864,
            "fast_sma_bars": 288,
            "slow_sma_bars": 1440,
            "require_trend_alignment": True,
            "minimum_breadth": 0.5,
            "top_n": 1,
            "gross_exposure": 0.4,
            "rebalance_bars": 288,
            "rebalance_offset": 144,
            "rebalance_threshold": 0.10,
            "stop_atr_period": 180,
        },
        research_evidence={
            **_BULLISH_5M_RESEARCH_METHOD,
            "strategy_revision": "2.0.0",
            "strategy_family": "long-only breadth-confirmed momentum rotation",
            "sharpe": 2.6261,
            "max_drawdown": -0.1758,
            "chronological_fold_method": (
                "fixed-parameter continuous jobs_v1 path divided into four "
                "contiguous quarters"
            ),
            "chronological_fold_returns": [0.5858, 0.0182, 0.5495, -0.0269],
            "hyperliquid_mechanism_check": {
                "bar_interval": "15m",
                "window_start": "2025-10-02T13:30:00+00:00",
                "window_end": "2026-08-25T00:00:00+00:00",
                "return_after_fees_and_slippage": 0.8949,
                "sharpe": 2.5219,
                "max_drawdown": -0.1748,
                "btc_bull_regime_return": 0.7200,
                "btc_bear_regime_return": 0.1017,
                "rebalance_phases_passing_return_and_sharpe_target": 57,
                "rebalance_phases_checked": 96,
            },
            "jobs_v1_engine": {
                "return_after_fees_and_slippage": 1.4347,
                "sharpe": 2.6261,
                "max_drawdown": -0.1758,
                "trade_count": 156,
                "total_fees_usd": 508.61,
                "stop_count": 0,
                "full_period_vs_no_stop": "unchanged",
                "chronological_folds_non_regressing": 4,
                "funding_included": False,
                "trace_valid": True,
            },
        },
        strategy_inception_at="2026-09-04T00:00:00+00:00",
        cautions=(
            "Native 5m validation used Binance USD-M candles; the same mechanism also passed on Hyperliquid 15m bars, but venue-specific forward behavior can differ.",
            "The newest chronological quarter lost 2.7%; this is a paper-first bullish specialist, not an all-regime claim.",
            "Funding is not included and the strategy can concentrate its full 40% target in one asset.",
            "Only the default 1x setting stayed within the -20% account-halt threshold in the leverage sweep.",
        ),
    ),
    StarterDefinition(
        id="diversified-trend-sleeves-15m",
        name="Diversified Trend Sleeves · 15m",
        family="cross_sectional_momentum",
        summary=(
            "Runs four independent relative-trend sleeves across underused crypto "
            "markets, so no single leader decides the whole portfolio."
        ),
        timeframe="15m",
        module="wayfinder_paths.jobs.strategies.mixed_sleeve_momentum",
        symbols=("HYPE", "DOGE", "ZEC", "SUI", "MORPHO", "AAVE", "PAXG", "AVAX"),
        crypto_assets=("HYPE", "DOGE", "ZEC", "SUI", "MORPHO", "AAVE", "PAXG", "AVAX"),
        tokenized_equities=(),
        rules=(
            "Compare trailing 3-day returns within HYPE/DOGE, ZEC/SUI, MORPHO/AAVE, and PAXG/AVAX.",
            "Long each sleeve winner and short its loser at 12.5% per leg; gross 100%, net 0%.",
            "Re-rank every 48 hours on a 00:00 UTC completed bar.",
        ),
        params={
            "sleeves": [
                ["HYPE", "DOGE"],
                ["ZEC", "SUI"],
                ["MORPHO", "AAVE"],
                ["PAXG", "AVAX"],
            ],
            "momentum_bars": 288,
            "rebalance_bars": 192,
            "rebalance_offset": 0,
            "weight_per_leg": 0.125,
            "rebalance_threshold": 0.10,
            "stop_atr_period": 96,
            "stop_atr_multiple": 20.0,
            "stop_min_pct": 0.60,
            "stop_max_pct": 0.80,
            "stop_cooldown_seconds": 0,
        },
        research_evidence={
            **_DIVERSE_INTRADAY_RESEARCH_METHOD,
            "strategy_revision": "2.0.0",
            "strategy_family": "cross-sectional sleeve momentum",
            "sharpe": 2.6780,
            "max_drawdown": -0.1107,
            "chronological_fold_method": (
                "fixed-parameter continuous jobs_v1 path divided into four "
                "contiguous quarters"
            ),
            "chronological_fold_returns": [0.1534, 0.1117, 0.3104, 0.0977],
            "phase_robustness": {
                "rebalance_phases_passing_return_and_sharpe_target": 159,
                "rebalance_phases_checked": 192,
                "full_period_sharpe_median": 2.0398,
            },
            "jobs_v1_engine": {
                "return_after_fees_and_slippage": 0.8443,
                "sharpe": 2.6780,
                "max_drawdown": -0.1107,
                "trade_count": 889,
                "total_fees_usd": 674.93,
                "stop_count": 0,
                "full_period_vs_no_stop": "unchanged",
                "chronological_folds_non_regressing": 4,
                "funding_included": False,
                "trace_valid": True,
            },
        },
        strategy_inception_at="2026-09-04T00:00:00+00:00",
        cautions=(
            "Funding is not included and four short legs can create material carry costs.",
            "The evidence spans roughly eleven months and includes an exceptional ZEC trend; forward diversification may be weaker.",
            "Only the default 1x setting stayed within the -20% account-halt threshold in the leverage sweep.",
        ),
    ),
    StarterDefinition(
        id="diversified-momentum-taker-15m",
        name="Diversified Momentum Taker · 15m",
        family="cross_sectional_momentum",
        summary=(
            "Trades only the strongest and weakest momentum tails of a broad "
            "ten-asset crypto panel using marketable rebalances."
        ),
        timeframe="15m",
        module="wayfinder_paths.jobs.strategies.mixed_momentum_rank",
        symbols=(
            "BNB",
            "DOGE",
            "SUI",
            "LINK",
            "AAVE",
            "AVAX",
            "PAXG",
            "HYPE",
            "ZEC",
            "MORPHO",
        ),
        crypto_assets=(
            "BNB",
            "DOGE",
            "SUI",
            "LINK",
            "AAVE",
            "AVAX",
            "PAXG",
            "HYPE",
            "ZEC",
            "MORPHO",
        ),
        tokenized_equities=(),
        rules=(
            "Rank all ten assets by trailing 5-day return.",
            "Long the top three and short the bottom three at one-sixth per leg; leave the middle four flat.",
            "Use taker orders to re-rank every 12 hours at 00:00 and 12:00 UTC.",
        ),
        params={
            "momentum_bars": 480,
            "rank_legs": 3,
            "rebalance_bars": 48,
            "rebalance_offset": 0,
            "weight_per_leg": 1 / 6,
            "rebalance_threshold": 0.10,
            "stop_atr_period": 96,
            "stop_atr_multiple": 20.0,
            "stop_min_pct": 0.60,
            "stop_max_pct": 0.80,
            "stop_cooldown_seconds": 0,
        },
        research_evidence={
            **_DIVERSE_INTRADAY_RESEARCH_METHOD,
            "strategy_revision": "2.0.0",
            "strategy_family": "broad cross-sectional taker momentum",
            "sharpe": 1.4236,
            "max_drawdown": -0.1821,
            "chronological_fold_method": (
                "fixed-parameter continuous jobs_v1 path divided into four "
                "contiguous quarters"
            ),
            "chronological_fold_returns": [0.0190, 0.1071, 0.3487, -0.0446],
            "phase_robustness": {
                "rebalance_phases_passing_return_and_sharpe_target": 40,
                "rebalance_phases_checked": 48,
                "full_period_sharpe_median": 1.7995,
            },
            "jobs_v1_engine": {
                "return_after_fees_and_slippage": 0.4537,
                "sharpe": 1.4236,
                "max_drawdown": -0.1821,
                "trade_count": 1634,
                "total_fees_usd": 1437.92,
                "stop_count": 0,
                "full_period_vs_no_stop": "unchanged",
                "chronological_folds_non_regressing": 4,
                "funding_included": False,
                "trace_valid": True,
            },
        },
        strategy_inception_at="2026-09-04T00:00:00+00:00",
        cautions=(
            "The full-period Sharpe only narrowly clears 1.4 and the newest chronological quarter lost 4.5%.",
            "This is an intentionally active taker strategy: the replay paid $1,438 in fees and modeled slippage on $10,000 initial capital.",
            "Funding is not included and can materially alter a persistent long/short basket.",
            "Only the default 1x setting stayed within the -20% account-halt threshold in the leverage sweep.",
        ),
    ),
    StarterDefinition(
        id="crypto-gold-regime-relay-15m",
        name="Crypto–Gold Regime Relay · 15m",
        family="regime_rotation",
        summary=(
            "Rotates between a concentrated crypto leader and tokenized gold, "
            "with cash as the fallback when neither side has positive momentum."
        ),
        timeframe="15m",
        module="wayfinder_paths.jobs.strategies.regime_rotation",
        symbols=("BNB", "HYPE", "ZEC", "MORPHO", "PAXG"),
        crypto_assets=("BNB", "HYPE", "ZEC", "MORPHO", "PAXG"),
        tokenized_equities=(),
        rules=(
            "Measure trailing 10-day momentum in BNB, HYPE, ZEC, and MORPHO.",
            "When at least two risk assets have positive momentum, own the strongest at 40% gross; otherwise own PAXG at 40% only if its own momentum is positive.",
            "Re-evaluate every eight hours at 00:00, 08:00, and 16:00 UTC; hold cash when neither side qualifies.",
        ),
        params={
            "risk_symbols": ["BNB", "HYPE", "ZEC", "MORPHO"],
            "defensive_symbol": "PAXG",
            "momentum_bars": 960,
            "require_trend_alignment": False,
            "minimum_breadth": 0.5,
            "top_n": 1,
            "gross_exposure": 0.4,
            "rebalance_bars": 32,
            "rebalance_offset": 0,
            "rebalance_threshold": 0.10,
            "stop_atr_period": 96,
        },
        research_evidence={
            **_DIVERSE_INTRADAY_RESEARCH_METHOD,
            "strategy_revision": "2.0.0",
            "strategy_family": "risk-on crypto / defensive gold relay",
            "sharpe": 1.6410,
            "max_drawdown": -0.1716,
            "chronological_fold_method": (
                "fixed-parameter continuous jobs_v1 path divided into four "
                "contiguous quarters"
            ),
            "chronological_fold_returns": [0.1737, 0.1426, 0.2226, 0.0384],
            "phase_robustness": {
                "rebalance_phases_passing_return_and_sharpe_target": 32,
                "rebalance_phases_checked": 32,
                "full_period_sharpe_minimum": 1.5580,
            },
            "jobs_v1_engine": {
                "return_after_fees_and_slippage": 0.7023,
                "sharpe": 1.6410,
                "max_drawdown": -0.1716,
                "trade_count": 276,
                "total_fees_usd": 634.06,
                "stop_count": 0,
                "full_period_vs_no_stop": "unchanged",
                "chronological_folds_non_regressing": 4,
                "funding_included": False,
                "trace_valid": True,
            },
        },
        strategy_inception_at="2026-09-04T00:00:00+00:00",
        cautions=(
            "PAXG is a traded asset, not cash; it can fall during risk-off periods and carries venue-specific basis and liquidity risk.",
            "Funding is not included and the evidence spans roughly eleven months.",
            "Only the default 1x setting stayed within the -20% account-halt threshold in the leverage sweep.",
        ),
    ),
    StarterDefinition(
        id="mixed-rsi-snapback-1h",
        name="Mixed RSI Snapback · 1h",
        family="mean_reversion",
        summary=(
            "Buys short, oversold pullbacks only while the asset remains above "
            "its long trend; otherwise holds cash."
        ),
        timeframe="1h",
        module="wayfinder_paths.jobs.strategies.mixed_rsi_snapback",
        symbols=("BTC", "HYPE", "xyz:COIN", "xyz:TSLA"),
        crypto_assets=("BTC", "HYPE"),
        tokenized_equities=("xyz:COIN", "xyz:TSLA"),
        rules=(
            "Enter long when RSI(6) is below 20 and close is above SMA(200).",
            "Exit when RSI(6) rises above 50 or after 72 completed bars.",
            "Target 25% per active leg; all four active legs target 100% gross.",
        ),
        params={
            "rsi_period": 6,
            "entry_rsi": 20.0,
            "exit_rsi": 50.0,
            "trend_sma_period": 200,
            "max_hold_bars": 72,
            "weight_per_leg": 0.25,
        },
        research_evidence={
            **_RESEARCH_METHOD,
            "window_start": "2025-11-25T17:00:00+00:00",
            "calendar_days": 264.2,
            "return_after_costs_and_funding": 0.0824,
            "funding_return_contribution": -0.0004,
            "sharpe": 1.66,
            "max_drawdown": -0.0279,
            "chronological_fold_returns": [0.0200, 0.0084, 0.0321, 0.0197],
            "jobs_v1_engine": {
                "return_after_fees_and_slippage": 0.0815,
                "sharpe": 1.63,
                "max_drawdown": -0.0279,
                "trade_count": 182,
                "total_fees_usd": 213.81,
                "stop_count": 0,
                "full_period_vs_no_stop": "unchanged",
                "chronological_folds_non_regressing": 4,
                "funding_included": False,
                "trace_valid": True,
            },
        },
    ),
    StarterDefinition(
        id="mixed-bollinger-pullback-1h",
        name="Mixed Bollinger Pullback · 1h",
        family="mean_reversion",
        summary=(
            "Fades unusually stretched moves back toward a three-day mean, "
            "but only in the direction of the asset's slower trend."
        ),
        timeframe="1h",
        module="wayfinder_paths.jobs.strategies.mixed_bollinger_pullback",
        symbols=("BTC", "SOL", "xyz:XYZ100", "xyz:TSLA"),
        crypto_assets=("BTC", "SOL"),
        tokenized_equities=("xyz:XYZ100", "xyz:TSLA"),
        rules=(
            "Standardize log price against its trailing 72-hour mean and volatility.",
            "Below -2z, buy only above SMA(200); above +2z, short only below SMA(200).",
            "Exit at the rolling mean or after 12 completed bars; target 25% per leg.",
        ),
        params={
            "zscore_bars": 72,
            "entry_zscore": 2.0,
            "exit_zscore": 0.0,
            "trend_sma_period": 200,
            "max_hold_bars": 12,
            "weight_per_leg": 0.25,
        },
        research_evidence={
            **_RESEARCH_METHOD,
            "window_start": "2025-11-13T14:00:00+00:00",
            "calendar_days": 276.4,
            "return_after_costs_and_funding": 0.0690,
            "funding_return_contribution": -0.0003,
            "sharpe": 2.13,
            "max_drawdown": -0.0151,
            "chronological_fold_returns": [0.0188, 0.0121, 0.0142, 0.0222],
            "signal_check": {
                "horizon_hours": 12,
                "training_events": 66,
                "training_excess_return": 0.0062,
                "training_t_stat": 2.85,
                "reserved_tail_events": 19,
                "reserved_tail_excess_return": 0.0050,
                "reserved_tail_t_stat": 1.35,
            },
            "jobs_v1_engine": {
                "return_after_fees_and_slippage": 0.0584,
                "sharpe": 1.85,
                "max_drawdown": -0.0162,
                "trade_count": 164,
                "total_fees_usd": 190.41,
                "stop_count": 0,
                "full_period_vs_no_stop": "unchanged",
                "chronological_folds_non_regressing": 4,
                "funding_included": False,
                "trace_valid": True,
            },
        },
        cautions=(
            "The 72-hour lookback was materially stronger than nearby lookbacks; treat this as a paper hypothesis and monitor decay.",
        ),
    ),
    StarterDefinition(
        id="mixed-volume-capitulation-1h",
        name="Mixed Volume Capitulation · 1h",
        family="mean_reversion",
        summary=(
            "Buys oversold pullbacks in established uptrends only when hourly "
            "volume confirms that the selloff is unusually active."
        ),
        timeframe="1h",
        module="wayfinder_paths.jobs.strategies.mixed_volume_capitulation",
        symbols=("BTC", "HYPE", "xyz:COIN", "xyz:TSLA"),
        crypto_assets=("BTC", "HYPE"),
        tokenized_equities=("xyz:COIN", "xyz:TSLA"),
        rules=(
            "Enter long when RSI(7) is below 20 and close remains above SMA(200).",
            "Require current hourly volume above its trailing 24-hour median.",
            "Exit when RSI(7) rises above 50 or after 72 completed bars; target 25% per leg.",
        ),
        params={
            "rsi_period": 7,
            "entry_rsi": 20.0,
            "exit_rsi": 50.0,
            "trend_sma_period": 200,
            "volume_median_bars": 24,
            "volume_multiple": 1.0,
            "max_hold_bars": 72,
            "weight_per_leg": 0.25,
        },
        research_evidence={
            **_RESEARCH_METHOD,
            "window_start": "2025-11-25T17:00:00+00:00",
            "calendar_days": 264.2,
            "return_after_costs_and_funding": 0.1214,
            "funding_return_contribution": 0.0006,
            "sharpe": 3.15,
            "max_drawdown": -0.0263,
            "chronological_fold_returns": [0.0377, 0.0158, 0.0344, 0.0284],
            "signal_check": {
                "horizon_hours": 8,
                "training_events": 51,
                "training_excess_return": 0.0076,
                "training_t_stat": 3.10,
                "reserved_tail_events": 5,
                "reserved_tail_excess_return": -0.0024,
                "reserved_tail_t_stat": -0.41,
            },
            "jobs_v1_engine": {
                "return_after_fees_and_slippage": 0.1230,
                "sharpe": 3.14,
                "max_drawdown": -0.0275,
                "trade_count": 104,
                "total_fees_usd": 124.52,
                "stop_count": 0,
                "full_period_vs_no_stop": "unchanged",
                "chronological_folds_non_regressing": 4,
                "funding_included": False,
                "trace_valid": True,
            },
        },
        cautions=(
            "The sparse raw trigger did not independently confirm in the reserved tail; forward evidence is especially important.",
        ),
    ),
    StarterDefinition(
        id="balanced-passive-capitulation-1h",
        name="Balanced Passive Capitulation · 1h",
        family="maker_mean_reversion",
        summary=(
            "Rests post-only bids on volume-confirmed oversold pullbacks across "
            "a balanced HYPE and tokenized-equity basket."
        ),
        timeframe="1h",
        module="wayfinder_paths.jobs.strategies.mixed_volume_capitulation",
        symbols=("HYPE", "xyz:COIN", "xyz:TSLA"),
        crypto_assets=("HYPE",),
        tokenized_equities=("xyz:COIN", "xyz:TSLA"),
        rules=(
            "Enter only when RSI(7) is below 20, close remains above SMA(200), and hourly volume exceeds its trailing 24-hour median.",
            "Rest an ALO bid 0.05 ATR(24) below the completed close for one hour; require 1 bp candle trade-through before counting a maker fill.",
            "Allocate 50% to HYPE and 25% to each equity perp; exit above RSI 50 or after 72 hours, with a fill-relative catastrophe stop and 24-hour stop cooldown.",
        ),
        params={
            "rsi_period": 7,
            "entry_rsi": 20.0,
            "exit_rsi": 50.0,
            "trend_sma_period": 200,
            "volume_median_bars": 24,
            "volume_multiple": 1.0,
            "max_hold_bars": 72,
            "weight_per_leg": 0.25,
            "symbol_weights": {
                "HYPE": 0.50,
                "xyz:COIN": 0.25,
                "xyz:TSLA": 0.25,
            },
            "entry_order_type": "maker",
            "entry_offset_atr": 0.05,
            "entry_ttl_bars": 1,
            "maker_fee_bps": 1.5,
            "maker_trade_through_bps": 1.0,
        },
        research_evidence={
            "source": (
                "Hydromancer Reservoir 1-second Hyperliquid HYPE and HIP-3 "
                "COIN/TSLA candles"
            ),
            "window_start": "2025-11-25T17:00:00+00:00",
            "window_end": "2026-08-18T23:00:00+00:00",
            "calendar_days": 266.25,
            "fill_model": (
                "decision on completed 1h close; post-only order first eligible "
                "on the next bar; require 1bp trade-through beyond the limit"
            ),
            "costs": {
                "maker_fee_bps_per_entry": 1.5,
                "taker_fee_bps_per_exit": 4.5,
                "taker_slippage_bps_per_exit": 3.5,
            },
            "funding_included": False,
            "funding": "not included; carry remains a live risk",
            "validation": (
                "development through 2026-01-19; four fixed chronological "
                "validation folds through 2026-06-21; one untouched reserved "
                "tail through 2026-08-18"
            ),
            "strategy_family": "passive diversified mean reversion",
            "sharpe": 3.67,
            "max_drawdown": -0.0149,
            "chronological_fold_returns": [0.0962, 0.0134, 0.0231, 0.0290],
            "walk_forward": {
                "oos_positive_folds": 4,
                "fold_count": 4,
                "oos_return_mean": 0.0404,
                "oos_sharpe_mean": 4.13,
                "newest_fold_return": 0.0290,
                "newest_fold_sharpe": 3.74,
            },
            "allocation_robustness": {
                "40_30_30": {
                    "return_after_fees_and_slippage": 0.1554,
                    "sharpe": 3.79,
                    "positive_validation_folds": 4,
                },
                "60_20_20": {
                    "return_after_fees_and_slippage": 0.2074,
                    "sharpe": 3.96,
                    "positive_validation_folds": 4,
                },
            },
            "reserved_tail": {
                "window_start": "2026-06-21T16:00:00+00:00",
                "window_end": "2026-08-18T23:00:00+00:00",
                "return_after_fees_and_slippage": 0.0155,
                "sharpe": 2.54,
                "max_drawdown": -0.0060,
                "trade_count": 16,
                "trace_valid": True,
            },
            "recent_120_day_replay": {
                "window_start": "2026-04-21T00:00:00+00:00",
                "return_after_fees_and_slippage": 0.0664,
                "sharpe": 3.41,
                "max_drawdown": -0.0124,
                "trade_count": 38,
            },
            "jobs_v1_engine": {
                "return_after_fees_and_slippage": 0.1995,
                "sharpe": 3.666,
                "max_drawdown": -0.0149,
                "trade_count": 78,
                "total_fees_usd": 77.31,
                "maker_entry_fills": 39,
                "taker_exit_fills": 39,
                "expired_entry_orders": 4,
                "stop_count": 0,
                "full_period_vs_no_stop": "unchanged",
                "chronological_folds_non_regressing": 4,
                "funding_included": False,
                "trace_valid": True,
            },
        },
        cautions=(
            "Funding is not included in the replay and can reduce live returns.",
            "Candle trade-through is conservative about touch fills but cannot reproduce exact queue position or partial fills.",
            "Live ALO routing is intentionally disabled until durable venue fill/cancel reconciliation is available.",
        ),
    ),
    StarterDefinition(
        id="mixed-momentum-rank-1h",
        name="Mixed Momentum Rank · 1h",
        family="cross_sectional_momentum",
        summary=(
            "A daily, market-neutral relative-strength basket across two "
            "crypto and two tokenized-equity markets."
        ),
        timeframe="1h",
        module="wayfinder_paths.jobs.strategies.mixed_momentum_rank",
        symbols=("BTC", "SOL", "xyz:XYZ100", "xyz:TSLA"),
        crypto_assets=("BTC", "SOL"),
        tokenized_equities=("xyz:XYZ100", "xyz:TSLA"),
        rules=(
            "Rank all four assets by trailing 14-day return.",
            "Long the top two and short the bottom two at 25% per leg.",
            "Re-rank daily on the 12:00 UTC completed bar; gross 100%, net 0%.",
        ),
        params={
            "momentum_bars": 336,
            "rebalance_bars": 24,
            "rebalance_offset": 12,
            "weight_per_leg": 0.25,
            "stop_atr_multiple": 8.0,
            "stop_min_pct": 0.15,
            "stop_max_pct": 0.30,
        },
        research_evidence={
            **_RESEARCH_METHOD,
            "window_start": "2025-11-13T14:00:00+00:00",
            "calendar_days": 276.3,
            "return_after_costs_and_funding": 0.2924,
            "funding_return_contribution": -0.0040,
            "sharpe": 1.76,
            "max_drawdown": -0.1075,
            "chronological_fold_returns": [0.0465, 0.0999, 0.0754, 0.0440],
            "jobs_v1_engine": {
                "return_after_fees_and_slippage": 0.2310,
                "sharpe": 1.56,
                "max_drawdown": -0.1096,
                "trade_count": 261,
                "total_fees_usd": 325.04,
                "stop_count": 0,
                "full_period_vs_no_stop": "unchanged",
                "chronological_folds_non_regressing": 4,
                "funding_included": False,
                "trace_valid": True,
            },
            "revalidation_note": (
                "The current-revision replay did not reproduce the earlier "
                "0.2675 return / 217-trade catalog snapshot. Its protected run "
                "exactly matched its reconstructed no-stop baseline, and these "
                "current-revision figures supersede the stale snapshot."
            ),
        },
    ),
    StarterDefinition(
        id="crypto-momentum-persistence-4h",
        name="Crypto Momentum Persistence · 4h",
        family="cross_sectional_momentum",
        summary=(
            "A concentrated crypto basket that owns the strongest risk-adjusted "
            "persistent trend, shorts the weakest, and leans long in broad rallies."
        ),
        timeframe="4h",
        module="wayfinder_paths.jobs.strategies.crypto_momentum_persistence",
        symbols=("BTC", "ETH", "SOL", "HYPE"),
        crypto_assets=("BTC", "ETH", "SOL", "HYPE"),
        tokenized_equities=(),
        rules=(
            "Blend trailing 7-day and 28-day returns equally, then divide by trailing 28-day volatility.",
            "Long the strongest and short the weakest at 35% each; gross 70%, net 0%.",
            "When all four raw momentum blends reach 10%, shift 17.5% from the short leg to the long; gross stays 70% and net becomes +35%.",
            "Re-rank daily on the 12:00 UTC completed bar.",
        ),
        params={
            "fast_momentum_bars": 42,
            "slow_momentum_bars": 168,
            "fast_momentum_weight": 0.5,
            "score_volatility_bars": 168,
            "rebalance_bars": 6,
            "rebalance_offset": 3,
            "weight_per_leg": 0.35,
            "broad_bull_momentum_threshold": 0.10,
            "broad_bull_weight_shift": 0.175,
            "stop_atr_period": 12,
        },
        research_evidence={
            **_CRYPTO_MOMENTUM_RESEARCH_METHOD,
            "return_after_fees_and_slippage": 1.0390,
            "sharpe": 1.5407,
            "max_drawdown": -0.1590,
            "chronological_fold_returns": [0.1004, 0.0772, 0.1534, 0.0400],
            "rank_admission": {
                "score": "equal 7-day/28-day return blend divided by trailing 28-day volatility",
                "forward_horizon_bars": 42,
                "information_coefficient": 0.0298,
                "t_stat": 3.049,
                "first_half_information_coefficient": 0.0200,
                "second_half_information_coefficient": 0.0396,
                "passed": True,
            },
            "broad_bull_overlay": {
                "activation": "all four raw momentum blends >= 0.10",
                "normal_weights": {"long": 0.35, "short": -0.35, "net": 0.0},
                "active_weights": {"long": 0.525, "short": -0.175, "net": 0.35},
                "gross_exposure": 0.70,
                "threshold_sharpe_sensitivity": {
                    "0.05": 1.3316,
                    "0.10": 1.5407,
                    "0.15": 1.4348,
                },
                "older_walk_forward_activation_count": 0,
            },
            "walk_forward": {
                "fold_count": 4,
                "positive_folds": 4,
                "mean_return_after_fees_and_slippage": 0.0928,
                "mean_sharpe": 2.1025,
                "worst_max_drawdown": -0.0939,
            },
            "rebalance_phase_returns": {
                "00:00_utc": 0.6834,
                "04:00_utc": 0.9323,
                "08:00_utc": 0.9043,
                "12:00_utc": 1.0390,
                "16:00_utc": 0.6709,
                "20:00_utc": 0.7512,
            },
            "rebalance_phase_sharpes": {
                "00:00_utc": 1.1362,
                "04:00_utc": 1.3922,
                "08:00_utc": 1.3795,
                "12:00_utc": 1.5407,
                "16:00_utc": 1.1407,
                "20:00_utc": 1.2191,
            },
            "recent_7_day_scenario": {
                "window_start": "2026-08-17T20:00:00+00:00",
                "window_end": "2026-08-24T16:00:00+00:00",
                "return_after_fees_and_slippage": 0.0447,
                "sharpe": 5.5413,
                "max_drawdown": -0.0235,
                "trade_count": 8,
                "total_fees_usd": 15.01,
                "initial_pair": {"long": "HYPE", "short": "BTC"},
                "trace_valid": True,
                "selection_role": "goal-directed development scenario, not holdout",
            },
            "jobs_v1_engine": {
                "return_after_fees_and_slippage": 1.0390,
                "sharpe": 1.5407,
                "max_drawdown": -0.1590,
                "trade_count": 436,
                "total_fees_usd": 900.88,
                "stop_count": 0,
                "full_period_vs_no_stop": "unchanged",
                "chronological_folds_non_regressing": 4,
                "funding_included": False,
                "trace_valid": True,
            },
        },
        cautions=(
            "Funding is not included in the replay and can reduce live returns.",
            "Broad-bull mode temporarily carries +35% net long exposure; the four older out-of-sample folds did not activate it.",
            "The recent seven-day scenario guided the overlay and is not out-of-sample.",
            "Only 1x stayed within the -20% account-halt threshold in the leverage sweep.",
        ),
    ),
    StarterDefinition(
        id="mixed-sleeve-momentum-15m",
        name="Crypto + Equity Sleeve Momentum · 15m",
        family="cross_sectional_momentum",
        summary=(
            "Keeps separate crypto and equity sleeves, long the stronger and "
            "short the weaker asset inside each sleeve."
        ),
        timeframe="15m",
        module="wayfinder_paths.jobs.strategies.mixed_sleeve_momentum",
        symbols=("BTC", "HYPE", "xyz:COIN", "xyz:MSTR"),
        crypto_assets=("BTC", "HYPE"),
        tokenized_equities=("xyz:COIN", "xyz:MSTR"),
        rules=(
            "Rank BTC vs HYPE and COIN vs MSTR by trailing 30-day return.",
            "Within each sleeve, long the winner and short the loser at 25% each.",
            "Re-rank daily on the 12:00 UTC completed bar; gross 100%, net 0%.",
        ),
        params={
            "momentum_bars": 2880,
            "rebalance_bars": 96,
            "rebalance_offset": 48,
            "weight_per_leg": 0.25,
            "stop_atr_multiple": 14.0,
            "stop_min_pct": 0.26,
            "stop_max_pct": 0.52,
        },
        research_evidence={
            **_RESEARCH_METHOD,
            "window_start": "2025-12-02T15:00:00+00:00",
            "calendar_days": 257.4,
            "return_after_costs_and_funding": 0.3375,
            "funding_return_contribution": -0.0031,
            "sharpe": 1.97,
            "max_drawdown": -0.1158,
            "chronological_fold_returns": [0.1136, -0.0545, 0.1963, 0.0619],
            "jobs_v1_engine": {
                "return_after_fees_and_slippage": 0.2587,
                "sharpe": 1.49,
                "max_drawdown": -0.1146,
                "trade_count": 120,
                "total_fees_usd": 141.12,
                "stop_count": 2,
                "full_period_vs_no_stop": "improved",
                "chronological_folds_non_regressing": 4,
                "no_stop_baseline": {
                    "return_after_fees_and_slippage": 0.2541,
                    "sharpe": 1.46,
                    "max_drawdown": -0.1170,
                },
                "funding_included": False,
                "trace_valid": True,
            },
        },
        cautions=("One of four chronological folds was negative.",),
    ),
    StarterDefinition(
        id="mixed-low-vol-rank-15m",
        name="Mixed Low-Volatility Rank · 15m",
        family="low_volatility_ranking",
        summary=(
            "A slow-moving defensive factor basket that owns the calmer pair "
            "and shorts the more volatile pair."
        ),
        timeframe="15m",
        module="wayfinder_paths.jobs.strategies.mixed_low_vol_rank",
        symbols=("BTC", "SOL", "xyz:XYZ100", "xyz:TSLA"),
        crypto_assets=("BTC", "SOL"),
        tokenized_equities=("xyz:XYZ100", "xyz:TSLA"),
        rules=(
            "Rank all four assets by trailing 5-day realized volatility.",
            "Long the two lowest-volatility assets and short the two highest.",
            "Re-rank daily on the 12:00 UTC completed bar; gross 100%, net 0%.",
        ),
        params={
            "volatility_bars": 480,
            "rebalance_bars": 96,
            "rebalance_offset": 48,
            "weight_per_leg": 0.25,
        },
        research_evidence={
            **_RESEARCH_METHOD,
            "window_start": "2025-11-13T14:30:00+00:00",
            "calendar_days": 276.4,
            "return_after_costs_and_funding": 0.1642,
            "funding_return_contribution": -0.0070,
            "sharpe": 1.03,
            "max_drawdown": -0.1301,
            "chronological_fold_returns": [0.0085, 0.0699, 0.0457, 0.0318],
            "jobs_v1_engine": {
                "return_after_fees_and_slippage": 0.2044,
                "sharpe": 1.34,
                "max_drawdown": -0.1023,
                "trade_count": 141,
                "total_fees_usd": 173.02,
                "stop_count": 0,
                "full_period_vs_no_stop": "unchanged",
                "chronological_folds_non_regressing": 4,
                "funding_included": False,
                "trace_valid": True,
            },
        },
    ),
    StarterDefinition(
        id="hype-passive-rsi-full-5m",
        name="HYPE Passive RSI · Full Exit · 5m",
        family="maker_mean_reversion",
        summary=(
            "Rests a deep post-only HYPE bid after an oversold close, then sells "
            "the full position at one maker target or exits on stop/time."
        ),
        timeframe="5m",
        module="wayfinder_paths.jobs.strategies.hype_passive_rsi",
        symbols=("HYPE",),
        crypto_assets=("HYPE",),
        tokenized_equities=(),
        rules=(
            "After a completed bar with RSI(14) at or below 30, rest an ALO bid 2 ATR(14) below the close for one bar.",
            "After fill, rest a full-position ALO sell 1.5 entry ATR above the fill.",
            "Use a fill-relative 3 ATR stop; otherwise close at market after four completed holding bars.",
        ),
        params={
            "rsi_period": 14,
            "entry_rsi": 30.0,
            "entry_offset_atr": 2.0,
            "entry_ttl_bars": 1,
            "exit_mode": "full",
            "take_profit_atr": 1.5,
            "max_hold_bars": 4,
            "stop_atr_period": 14,
            "stop_atr_multiple": 3.0,
            "stop_min_pct": 0.001,
            "stop_max_pct": 0.20,
            "native_stop_required": True,
            "maker_fee_bps": 1.5,
            "maker_trade_through_bps": 1.0,
        },
        research_evidence={
            **_MAKER_RESEARCH_METHOD,
            "strategy_family": "passive directional mean reversion",
            "sharpe": 1.66,
            "max_drawdown": -0.0627,
            "chronological_fold_returns": [0.0112, 0.0133, 0.0243, -0.0072],
            "signal_check": {
                "signal": "HYPE RSI(14) <= 30",
                "horizon_minutes": 10,
                "training_events": 2148,
                "training_t_stat": 4.729,
                "pooled_bh_q_value": 0.0072,
                "training_folds_positive": 4,
                "reserved_tail_events": 354,
                "reserved_tail_t_stat": 1.066,
                "reserved_tail_hit_rate": 0.5537,
            },
            "walk_forward": {
                "oos_positive_folds": 3,
                "fold_count": 4,
                "oos_return_mean": 0.0104,
                "oos_sharpe_mean": 0.88,
                "newest_fold_return": -0.0072,
                "newest_fold_sharpe": -1.00,
            },
            "recent_120_day_replay": {
                "window_start": "2026-04-20T23:55:00+00:00",
                "return_after_fees_and_slippage": 0.0236,
                "sharpe": 0.98,
                "max_drawdown": -0.0281,
                "trade_count": 68,
            },
            "jobs_v1_engine": {
                "return_after_fees_and_slippage": 0.2566,
                "sharpe": 1.6639,
                "max_drawdown": -0.0627,
                "trade_count": 244,
                "total_fees_usd": 613.80,
                "stop_count": 15,
                "full_period_vs_no_stop": "improved",
                "chronological_folds_non_regressing": 2,
                "stop_vs_no_stop_fold_return_deltas": [
                    0.0036,
                    0.0005,
                    -0.0035,
                    -0.0055,
                ],
                "no_stop_baseline": {
                    "return_after_fees_and_slippage": 0.2531,
                    "sharpe": 1.5170,
                    "max_drawdown": -0.0657,
                },
                "funding_included": False,
                "trace_valid": True,
            },
        },
        cautions=(
            "The newest held-out fold was negative and the full-period Sharpe remained below 2; paper-forward evidence is required.",
            "Candle trade-through is conservative about touch fills but cannot reproduce exact queue position or partial fills.",
            "Live ALO routing is intentionally disabled until durable venue fill/cancel reconciliation is available.",
        ),
    ),
    StarterDefinition(
        id="hype-passive-rsi-staged-5m",
        name="HYPE Passive RSI · Staged Exit · 5m",
        family="maker_mean_reversion",
        summary=(
            "Uses the same deep post-only HYPE entry, sells half at the first "
            "maker target, then lets the balance seek a second target."
        ),
        timeframe="5m",
        module="wayfinder_paths.jobs.strategies.hype_passive_rsi",
        symbols=("HYPE",),
        crypto_assets=("HYPE",),
        tokenized_equities=(),
        rules=(
            "After a completed bar with RSI(14) at or below 30, rest an ALO bid 2 ATR(14) below the close for one bar.",
            "Sell 50% at 1 entry ATR and the remainder at 1.5 ATR using ALO orders; keep the original stop on the remainder.",
            "Use an initial fill-relative 3 ATR stop; otherwise close the remainder at market after four completed holding bars.",
        ),
        params={
            "rsi_period": 14,
            "entry_rsi": 30.0,
            "entry_offset_atr": 2.0,
            "entry_ttl_bars": 1,
            "exit_mode": "staged",
            "take_profit_one_atr": 1.0,
            "take_profit_two_atr": 1.5,
            "take_profit_one_fraction": 0.5,
            "move_stop_to_break_even": False,
            "max_hold_bars": 4,
            "stop_atr_period": 14,
            "stop_atr_multiple": 3.0,
            "stop_min_pct": 0.001,
            "stop_max_pct": 0.20,
            "native_stop_required": True,
            "maker_fee_bps": 1.5,
            "maker_trade_through_bps": 1.0,
        },
        research_evidence={
            **_MAKER_RESEARCH_METHOD,
            "strategy_family": "passive directional mean reversion",
            "sharpe": 1.58,
            "max_drawdown": -0.0653,
            "chronological_fold_returns": [0.0162, 0.0118, 0.0361, -0.0106],
            "signal_check": {
                "signal": "HYPE RSI(14) <= 30",
                "horizon_minutes": 10,
                "training_events": 2148,
                "training_t_stat": 4.729,
                "pooled_bh_q_value": 0.0072,
                "training_folds_positive": 4,
                "reserved_tail_events": 354,
                "reserved_tail_t_stat": 1.066,
                "reserved_tail_hit_rate": 0.5537,
            },
            "walk_forward": {
                "oos_positive_folds": 3,
                "fold_count": 4,
                "oos_return_mean": 0.0134,
                "oos_sharpe_mean": 1.32,
                "newest_fold_return": -0.0106,
                "newest_fold_sharpe": -1.53,
            },
            "recent_120_day_replay": {
                "window_start": "2026-04-20T23:55:00+00:00",
                "return_after_fees_and_slippage": 0.0317,
                "sharpe": 1.44,
                "max_drawdown": -0.0285,
                "trade_count": 94,
            },
            "jobs_v1_engine": {
                "return_after_fees_and_slippage": 0.2299,
                "sharpe": 1.5785,
                "max_drawdown": -0.0653,
                "trade_count": 334,
                "total_fees_usd": 561.83,
                "stop_count": 15,
                "full_period_vs_no_stop": "improved",
                "chronological_folds_non_regressing": 1,
                "stop_vs_no_stop_fold_return_deltas": [
                    0.0013,
                    -0.0003,
                    -0.0018,
                    -0.0055,
                ],
                "no_stop_baseline": {
                    "return_after_fees_and_slippage": 0.2279,
                    "sharpe": 1.4592,
                    "max_drawdown": -0.0683,
                },
                "funding_included": False,
                "trace_valid": True,
            },
        },
        cautions=(
            "The newest held-out fold was negative and the full-period Sharpe remained below 2; paper-forward evidence is required.",
            "Candle trade-through is conservative about touch fills but cannot reproduce exact queue position or partial fills.",
            "Live ALO routing is intentionally disabled until durable venue fill/cancel reconciliation is available.",
        ),
    ),
    StarterDefinition(
        id="btc-eth-relative-strength-1d",
        name="BTC / ETH Relative Strength · 1d",
        family="relative_value_pair",
        summary=(
            "A high-liquidity pair trade that owns the stronger major and "
            "shorts the weaker one while targeting stable spread risk."
        ),
        timeframe="1d",
        module="wayfinder_paths.jobs.strategies.pair_relative_strength",
        symbols=("BTC", "ETH"),
        crypto_assets=("BTC", "ETH"),
        tokenized_equities=(),
        rules=(
            "Compare trailing 90-day BTC and ETH log returns.",
            "Long the relative-strength leader and short the laggard; rebalance weekly.",
            "Target 10% annualized spread volatility using 28 days of history; clamp gross exposure to 15–100%.",
        ),
        params={
            "momentum_bars": 90,
            "volatility_bars": 28,
            "bars_per_year": 365,
            "target_volatility": 0.10,
            "min_gross_exposure": 0.15,
            "max_gross_exposure": 1.0,
            "rebalance_bars": 7,
            "rebalance_offset": 4,
        },
        research_evidence={
            **_PAIR_RESEARCH_METHOD,
            "strategy_family": "cross-sectional pair momentum",
            "return_after_costs_and_funding": 0.2185,
            "funding_return_contribution": -0.0010,
            "sharpe": 1.10,
            "max_drawdown": -0.1031,
            "chronological_fold_returns": [0.1443, 0.0318, 0.0249, 0.0070],
            "weekly_phase_sharpes_before_funding": [
                1.00,
                1.15,
                1.07,
                0.92,
                1.10,
                0.72,
                0.46,
            ],
            "price_mean_reversion_gate": {
                "verdict": "REJECT",
                "failed": [
                    "engle_granger_both_directions",
                    "half_life",
                    "rolling_stability",
                ],
                "engle_granger_t_stats": {"btc_on_eth": -2.286, "eth_on_btc": -1.957},
                "half_life_hours": 2291.1,
                "rolling_stability_fraction": 0.26,
                "interpretation": (
                    "This is deliberately a relative-momentum pair, not a "
                    "z-score mean-reversion strategy."
                ),
            },
            "jobs_v1_engine": {
                "return_after_fees_and_slippage": 0.2090,
                "sharpe": 0.99,
                "max_drawdown": -0.1102,
                "trade_count": 182,
                "total_fees_usd": 56.43,
                "stop_count": 0,
                "full_period_vs_no_stop": "unchanged",
                "chronological_folds_non_regressing": 4,
                "funding_included": False,
                "trace_valid": True,
            },
        },
        cautions=(
            "This is a relative-momentum pair, not a price mean-reversion strategy.",
        ),
    ),
    StarterDefinition(
        id="bch-ltc-relative-strength-1d",
        name="BCH / LTC Relative Strength · 1d",
        family="relative_value_pair",
        summary=(
            "A proof-of-work relative-value pair that rotates toward the "
            "stronger coin and scales down when the spread becomes volatile."
        ),
        timeframe="1d",
        module="wayfinder_paths.jobs.strategies.pair_relative_strength",
        symbols=("BCH", "LTC"),
        crypto_assets=("BCH", "LTC"),
        tokenized_equities=(),
        rules=(
            "Compare trailing 90-day BCH and LTC log returns.",
            "Long the relative-strength leader and short the laggard; rebalance weekly.",
            "Target 10% annualized spread volatility using 28 days of history; clamp gross exposure to 15–100%.",
        ),
        params={
            "momentum_bars": 90,
            "volatility_bars": 28,
            "bars_per_year": 365,
            "target_volatility": 0.10,
            "min_gross_exposure": 0.15,
            "max_gross_exposure": 1.0,
            "rebalance_bars": 7,
            "rebalance_offset": 4,
        },
        research_evidence={
            **_PAIR_RESEARCH_METHOD,
            "strategy_family": "cross-sectional pair momentum",
            "return_after_costs_and_funding": 0.3054,
            "funding_return_contribution": 0.0007,
            "sharpe": 1.43,
            "max_drawdown": -0.1078,
            "chronological_fold_returns": [0.0222, 0.0354, 0.0642, 0.1590],
            "weekly_phase_sharpes_before_funding": [
                0.93,
                1.41,
                1.09,
                1.15,
                1.43,
                1.31,
                1.16,
            ],
            "price_mean_reversion_gate": {
                "verdict": "REJECT",
                "failed": [
                    "engle_granger_both_directions",
                    "half_life",
                    "rolling_stability",
                ],
                "engle_granger_t_stats": {"bch_on_ltc": -1.235, "ltc_on_bch": -1.479},
                "half_life_hours": 3064.0,
                "rolling_stability_fraction": 0.33,
                "interpretation": (
                    "This is deliberately a relative-momentum pair, not a "
                    "z-score mean-reversion strategy."
                ),
            },
            "jobs_v1_engine": {
                "return_after_fees_and_slippage": 0.2815,
                "sharpe": 1.24,
                "max_drawdown": -0.1079,
                "trade_count": 181,
                "total_fees_usd": 49.76,
                "stop_count": 0,
                "full_period_vs_no_stop": "unchanged",
                "chronological_folds_non_regressing": 4,
                "funding_included": False,
                "trace_valid": True,
            },
        },
        cautions=(
            "This is a relative-momentum pair, not a price mean-reversion strategy.",
            "BCH and LTC are materially less liquid than BTC and ETH; keep this starter small and honor live capacity checks.",
        ),
    ),
    StarterDefinition(
        id="diversified-funding-oi-divergence-maker-15m",
        name="Diversified Funding / OI Divergence Maker · 15m",
        family="funding_divergence",
        summary=(
            "Rests post-only orders against a crowded side that hourly funding "
            "says is paying up, price is not rewarding, and open interest shows "
            "still adding, across nineteen Hyperliquid perps."
        ),
        timeframe="15m",
        module="wayfinder_paths.jobs.strategies.mixed_funding_divergence",
        symbols=_FUNDING_OI_DIVERGENCE_SYMBOLS,
        crypto_assets=_FUNDING_OI_DIVERGENCE_SYMBOLS,
        tokenized_equities=(),
        rules=(
            "Score hourly Hyperliquid funding as a z-score over the trailing 30 days (2,880 bars); a reading beyond ±2 marks a crowded side.",
            "Fade the crowd only while price has not rewarded it over the trailing 24 hours and open interest has grown over the same 24 hours.",
            "Rest a post-only order 0.5 ATR(24) beyond the close, replaced every bar; hold each fill for 96 completed bars or until the signal flips, then exit with a marketable order.",
            "Size every leg at 5% of equity; the catastrophe stop is 12x ATR(24) bounded to 30–50%.",
        ),
        params={
            **_FUNDING_OI_DIVERGENCE_PARAMS,
            "entry_order_type": "maker",
            "entry_offset_atr": 0.5,
            "entry_ttl_bars": 1,
        },
        research_evidence={
            **_FUNDING_OI_DIVERGENCE_RESEARCH_METHOD,
            "strategy_family": (
                "funding-rate divergence fade with open-interest confirmation, "
                "passive entries"
            ),
            "sharpe": 0.7049,
            "max_drawdown": -0.0577,
            "chronological_fold_method": (
                "fixed-parameter continuous jobs_v1 path divided into four "
                "contiguous quarters"
            ),
            "chronological_fold_returns": [0.0093, 0.0102, -0.0131, 0.0402],
            "cross_asset_lift": {
                "method": (
                    "jobs_v1 engine per symbol on cached frames, daily returns "
                    "pooled at equal weight, 2025-08-31 to 2026-09-04"
                ),
                "pooled_sharpe": 0.77,
                "pooled_return": 0.0474,
                "halves_sharpe": [0.89, 0.62],
                "symbols_positive": "12 of 19",
                "funding_only_pooled_sharpe": 0.25,
                "open_interest_unwind_pooled_sharpe": -0.25,
            },
            "signal_screen": {
                "hyperliquid_pooled_sharpe": 1.75,
                "binance_same_year_funding_only_sharpe": 1.13,
                "binance_four_year_funding_only_sharpe": -0.20,
                "binance_funding_only_by_year": {
                    "2022": -0.58,
                    "2023": 0.71,
                    "2024": -1.75,
                    "2025": 1.29,
                },
                "note": (
                    "screen figures used a research fill model that rested a fresh "
                    "order at every signal bar and overstated passive edges; the "
                    "engine figures above are the published basis"
                ),
            },
            "funding_pnl_note": (
                "with hourly funding P&L included the same path returned 6.03% "
                "at Sharpe 0.90 (drawdown -5.71%): the fade collects funding"
            ),
            "jobs_v1_engine": {
                "return_after_fees_and_slippage": 0.0467,
                "sharpe": 0.7049,
                "max_drawdown": -0.0577,
                "trade_count": 686,
                "maker_fills": 343,
                "total_fees_usd": 105.39,
                "stop_count": 0,
                "full_period_vs_no_stop": "unchanged",
                "chronological_folds_non_regressing": 4,
                "funding_included": False,
                "trace_valid": True,
            },
        },
        strategy_inception_at="2026-09-05T00:00:00+00:00",
        cautions=(
            *_FUNDING_OI_DIVERGENCE_CAUTIONS,
            "Maker fills use the strict candle trade-through model; live limit routing stays disabled until durable venue fill/cancel reconciliation lands.",
            "Only 1x to 3x stayed within the -20% account-halt threshold in the leverage sweep.",
        ),
        features=_FUNDING_OI_DIVERGENCE_FEATURES,
    ),
    StarterDefinition(
        id="diversified-funding-oi-divergence-taker-15m",
        name="Diversified Funding / OI Divergence Taker · 15m",
        family="funding_divergence",
        summary=(
            "Takes the next open against a crowded side that hourly funding "
            "says is paying up, price is not rewarding, and open interest shows "
            "still adding, across nineteen Hyperliquid perps."
        ),
        timeframe="15m",
        module="wayfinder_paths.jobs.strategies.mixed_funding_divergence",
        symbols=_FUNDING_OI_DIVERGENCE_SYMBOLS,
        crypto_assets=_FUNDING_OI_DIVERGENCE_SYMBOLS,
        tokenized_equities=(),
        rules=(
            "Score hourly Hyperliquid funding as a z-score over the trailing 30 days (2,880 bars); a reading beyond ±2 marks a crowded side.",
            "Fade the crowd only while price has not rewarded it over the trailing 24 hours and open interest has grown over the same 24 hours.",
            "Enter at the next bar open; hold 96 completed bars or until the signal flips, then exit with a marketable order.",
            "Size every leg at 5% of equity; the catastrophe stop is 12x ATR(24) bounded to 30–50%.",
        ),
        params={
            **_FUNDING_OI_DIVERGENCE_PARAMS,
            "entry_order_type": "market",
        },
        research_evidence={
            **_FUNDING_OI_DIVERGENCE_RESEARCH_METHOD,
            "strategy_family": (
                "funding-rate divergence fade with open-interest confirmation, "
                "market entries"
            ),
            "sharpe": 0.8755,
            "max_drawdown": -0.0475,
            "chronological_fold_method": (
                "fixed-parameter continuous jobs_v1 path divided into four "
                "contiguous quarters"
            ),
            "chronological_fold_returns": [0.0115, 0.025, -0.0073, 0.033],
            "cross_asset_lift": {
                "method": (
                    "jobs_v1 engine per symbol on cached frames, daily returns "
                    "pooled at equal weight, 2025-08-31 to 2026-09-04"
                ),
                "pooled_sharpe": 0.93,
                "pooled_return": 0.0633,
                "halves_sharpe": [1.21, 0.59],
                "symbols_positive": "8 of 19",
                "funding_only_pooled_sharpe": 0.30,
                "open_interest_unwind_pooled_sharpe": -0.63,
            },
            "signal_screen": {
                "hyperliquid_pooled_sharpe": 0.99,
                "binance_same_year_funding_only_sharpe": 1.13,
                "binance_four_year_funding_only_sharpe": -0.20,
                "binance_funding_only_by_year": {
                    "2022": -0.58,
                    "2023": 0.71,
                    "2024": -1.75,
                    "2025": 1.29,
                },
            },
            "funding_pnl_note": (
                "with hourly funding P&L included the same path returned 7.74% "
                "at Sharpe 1.06 (drawdown -4.69%): the fade collects funding"
            ),
            "jobs_v1_engine": {
                "return_after_fees_and_slippage": 0.0632,
                "sharpe": 0.8755,
                "max_drawdown": -0.0475,
                "trade_count": 792,
                "total_fees_usd": 184.94,
                "stop_count": 0,
                "full_period_vs_no_stop": "unchanged",
                "chronological_folds_non_regressing": 4,
                "funding_included": False,
                "trace_valid": True,
            },
        },
        strategy_inception_at="2026-09-05T00:00:00+00:00",
        cautions=(
            *_FUNDING_OI_DIVERGENCE_CAUTIONS,
            "Only 1x to 4x stayed within the -20% account-halt threshold in the leverage sweep.",
        ),
        features=_FUNDING_OI_DIVERGENCE_FEATURES,
    ),
)


def starter_catalog() -> list[dict[str, Any]]:
    return [definition.to_dict() for definition in STARTER_DEFINITIONS]


def get_starter(starter_id: str) -> StarterDefinition:
    normalized = str(starter_id).strip().lower()
    for definition in STARTER_DEFINITIONS:
        if definition.id == normalized:
            return definition
    raise KeyError(f"unknown starter strategy: {starter_id}")


def _spawn_starter_dataset_fetch(store: JobStore, job_id: str) -> dict[str, Any]:
    """Self-provision the starter's market dataset as a detached fetch.

    Launch stays fast (the child fetches bars minutes later into
    results/backtest/input_bars.json); until then, backtests report the
    in-progress fetch instead of a bare "no bars" error. Any failure here is
    journaled and swallowed — dataset provisioning must never fail the launch.
    """
    try:
        bars_path = store.job_dir(job_id) / "results" / "backtest" / "input_bars.json"
        if bars_path.exists():
            store.append_journal(
                job_id,
                {"type": "starter_dataset_fetch_skipped", "reason": "dataset_exists"},
            )
            return {"spawned": False, "reason": "dataset_exists"}
        status = spawn_detached_op(
            store,
            job_id,
            "fetch_dataset",
            {
                "job_id": job_id,
                "days": STARTER_DATASET_DAYS,
                "exchange": "hyperliquid",
                "quote": "USDC",
                "include_funding": True,
            },
        )
        if status.get("already_running"):
            store.append_journal(
                job_id,
                {
                    "type": "starter_dataset_fetch_skipped",
                    "reason": "fetch_already_running",
                },
            )
            return {"spawned": False, "reason": "fetch_already_running"}
        store.append_journal(
            job_id,
            {
                "type": "starter_dataset_fetch_spawned",
                "op": "fetch_dataset",
                "days": STARTER_DATASET_DAYS,
                "pid": status.get("pid"),
            },
        )
        return {"spawned": True, "days": STARTER_DATASET_DAYS, "pid": status.get("pid")}
    except Exception as exc:  # noqa: BLE001 — never block or fail the launch
        try:
            store.append_journal(
                job_id,
                {"type": "starter_dataset_fetch_spawn_failed", "error": str(exc)},
            )
        except Exception:  # noqa: BLE001
            pass
        return {"spawned": False, "error": str(exc)}


def create_starter_job(
    starter_id: str,
    *,
    job_id: str | None = None,
    store: JobStore | None = None,
    compile_job: bool = True,
    initializer_session_id: str | None = None,
    leverage: int | float | None = None,
    agent_mode: str | None = None,
) -> dict[str, Any]:
    """Materialize a selectable starter as an ordinary paper jobs_v1 job."""
    definition = get_starter(starter_id)
    store = store or JobStore()
    resolved_id = safe_job_id(job_id or definition.id)
    job_path = store.job_dir(resolved_id) / "job.yaml"
    if job_path.exists():
        existing = store.load(resolved_id)
        existing_starter = existing.controller.get("starter") or {}
        if job_id is not None or existing_starter.get("id") != definition.id:
            raise FileExistsError(f"job already exists: {resolved_id}")
        entrypoint = store.resolve_script_entrypoint(existing.id, existing.to_dict())
        selected_leverage, leverage_warning = coerce_starter_leverage(
            existing.execution_params.get("leverage", STARTER_LEVERAGE_DEFAULT)
        )
        return {
            "created": False,
            "job": existing.to_dict(),
            "job_yaml": str(job_path),
            "script_entrypoint": str(entrypoint) if entrypoint is not None else None,
            "starter": definition.to_dict(),
            "selected_leverage": selected_leverage,
            "leverage_warning": leverage_warning,
        }

    from wayfinder_paths.jobs.execution.primitives import bar_interval_seconds

    configured_params = definition.configured_params()
    selected_leverage = validate_starter_leverage(
        STARTER_LEVERAGE_DEFAULT if leverage is None else leverage
    )
    interval_seconds = int(bar_interval_seconds(definition.timeframe) or 0)
    if interval_seconds <= 0:
        raise ValueError(f"unsupported starter timeframe: {definition.timeframe}")
    # None → catalog default (intervene). An explicit mode is honored so
    # operator plumbing (CLI/MCP) can launch differently on purpose.
    launch_agent_mode = (
        normalize_agent_mode(agent_mode)
        if agent_mode is not None
        else STARTER_AGENT_MODE_DEFAULT
    )
    job = WayfinderJob.new(
        resolved_id,
        name=definition.name,
        goal=(
            f"Paper-track the {definition.name} starter from this job's inception; "
            "refresh its backtest before any proposal to change risk or go live."
        ),
        script="workspace/src/strategy.py",
        interval_seconds=interval_seconds,
        timeout_seconds=180,
        agent_mode=launch_agent_mode,
        agent_wake_seconds=STARTER_AGENT_WAKE_SECONDS,
        execution_contract="jobs_v1",
        initializer_session_id=initializer_session_id,
    )
    job.execution_spec = {
        "market_kind": "perp",
        "view_type": "completed_bars",
        "bar_model": "completed_only",
        "fill_model": "next_bar_open",
        "ohlc_rules": {
            "use_high_low_for_stops": True,
            "allow_close_only_entries": False,
            "same_bar_fill": False,
            "same_bar_policy": "conservative",
        },
        "data_contract": {
            "candles_source": "sdk_only",
            "no_external_ccxt": True,
            "rate_limit_safe": True,
            "bar_interval": definition.timeframe,
            "symbols": list(definition.symbols),
            "max_bar_age_intervals": 2,
            "stale_policy": "skip",
            **(
                {"features": [copy.deepcopy(dict(f)) for f in definition.features]}
                if definition.features
                else {}
            ),
        },
        "validation": {
            "mode": "strict",
            "require_scenarios": False,
            **(
                {
                    "robustness_plan": copy.deepcopy(
                        STARTER_ROBUSTNESS_PLANS[definition.id]
                    )
                }
                if definition.id in STARTER_ROBUSTNESS_PLANS
                else {}
            ),
        },
        "venues": ["hyperliquid"],
    }
    job.execution_params = {
        **configured_params,
        "symbols": list(definition.symbols),
        "venue": "hyperliquid",
        "initial_capital": 10_000.0,
        "fee_bps": 4.5,
        "slippage_bps": 3.5,
        "min_trade_notional": 25.0,
        "leverage": selected_leverage,
        # Without this the driver's default 200-bar window caps ctx.bar_index
        # below warmup for most starters and they never trade.
        "lookback_bars": starter_lookback_bars(definition),
    }
    job.controller["starter"] = {
        "id": definition.id,
        "catalog_version": STARTER_CATALOG_VERSION,
        "strategy_inception_at": definition.strategy_inception_at,
        "job_tracking_inception_at": job.created_at,
        "paper_only": True,
        "risk_limits": definition.risk_limits(),
        "selected_leverage": selected_leverage,
    }
    job.performance["starter_evidence"] = "results/backtest/starter_evidence.json"
    job.performance["tracking_inception_at"] = job.created_at

    job_path = store.create_job(job)
    store.write_json(job.id, "workspace/risk_limits.json", definition.risk_limits())
    entrypoint = store.resolve_script_entrypoint(job.id, job.to_dict())
    if entrypoint is None:
        raise RuntimeError("starter job has no workspace strategy entrypoint")
    Path(entrypoint).write_text(
        f"from {definition.module} import build_strategy\n",
        encoding="utf-8",
    )
    evidence = definition.to_dict()
    evidence["job_id"] = job.id
    evidence["job_tracking_inception_at"] = job.created_at
    evidence["selected_leverage"] = selected_leverage
    store.write_json(job.id, "results/backtest/starter_evidence.json", evidence)

    result: dict[str, Any] = {
        "created": True,
        "job": job.to_dict(),
        "job_yaml": str(job_path),
        "script_entrypoint": str(entrypoint),
        "starter": definition.to_dict(),
        "selected_leverage": selected_leverage,
    }
    if compile_job:
        from wayfinder_paths.jobs.compiler import JobCompiler
        from wayfinder_paths.jobs.sync import sync_all_jobs

        result["compile"] = JobCompiler(store=store).compile(job)
        sync_all_jobs(store=store)
    result["dataset_fetch"] = _spawn_starter_dataset_fetch(store, job.id)
    return result
