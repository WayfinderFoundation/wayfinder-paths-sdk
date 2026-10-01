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
from wayfinder_paths.jobs.execution.spec_defaults import (
    harnessed_execution_params,
    harnessed_execution_spec,
)
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

STARTER_CATALOG_VERSION = "2.1.0"
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
    # Retirement affects new canned launches, not existing jobs or research
    # imports. Historical definitions remain resolvable by their stable ID.
    selectable: bool = True

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
            "jobs_v1_leverage_sweep": copy.deepcopy(
                self.research_evidence["jobs_v1_leverage_sweep"]
            )
            if "jobs_v1_leverage_sweep" in self.research_evidence
            else {
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
                "selectable": self.selectable,
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
        selectable=False,
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
            "Runs four relative-trend sleeves, sizing each by its trailing "
            "relative volatility and capping each sleeve's gross exposure."
        ),
        timeframe="15m",
        module="wayfinder_paths.jobs.strategies.risk_balanced_sleeves",
        symbols=("HYPE", "DOGE", "ZEC", "SUI", "MORPHO", "AAVE", "PAXG", "AVAX"),
        crypto_assets=("HYPE", "DOGE", "ZEC", "SUI", "MORPHO", "AAVE", "PAXG", "AVAX"),
        tokenized_equities=(),
        rules=(
            "Compare trailing 3-day returns within HYPE/DOGE, ZEC/SUI, MORPHO/AAVE, and PAXG/AVAX.",
            "Long each sleeve winner and short its loser equally; allocate inverse 20-day relative volatility, capped at 35% gross per sleeve.",
            "Gross exposure is at most 100%, net 0%; capped allocation remains in cash rather than being redistributed.",
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
            "risk_window_bars": 1920,
            "max_sleeve_gross": 0.35,
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
            "strategy_revision": "2.1.0",
            "source": "Verified Hydromancer/Hyperliquid candles and hourly funding; quiet archive "
            "intervals carry the previous close at zero volume without invented fills.",
            "window_start": "2025-09-27 00:15:00+00:00",
            "window_end": "2026-09-27 00:00:00+00:00",
            "evidence_role": "inspected_historical_development_not_sealed",
            "costs": {"taker_fee_bps_per_side": 4.5, "slippage_bps_per_side": 7.0},
            "funding_included": True,
            "funding": "Verified hourly settlements by signed exposure; no zero-fill of missing "
            "funding.",
            "fill_model": "Completed-bar decisions, next-bar fills, OHLC stops; passive orders "
            "require strict trade-through.",
            "return_after_costs_and_funding": 0.7981238934487129,
            "sharpe": 2.6822238778203613,
            "max_drawdown": -0.09410183890458439,
            "jobs_v1_engine": {
                "return_after_fees_and_slippage": 0.7981238934487129,
                "sharpe": 2.6822238778203613,
                "sortino": 3.761030482304074,
                "max_drawdown": -0.09410183890458439,
                "trade_count": 956,
                "total_fees_usd": 720.363906053441,
                "total_funding_usd": -81.47478394890821,
                "funding_included": True,
                "trace_valid": True,
            },
            "jobs_v1_leverage_sweep": {
                "leverage_semantics": "target_exposure",
                "account_halt_simulated": False,
                "qualification_scope": "default_1x_only",
                "results": STARTER_LEVERAGE_RESULTS["diversified-trend-sleeves-15m"],
            },
            "qualification": {
                "profile": "starter-paper-2026-09",
                "quick_screen_passed": True,
                "full_development_passed": True,
                "paper_admission_passed": True,
                "reference": "cash",
                "leverage": 1,
                "forward_probation": "not_run",
                "live_promotion": "not_authorized",
                "policy_overrides": {
                    "full_dev_haircut_blocking": False,
                    "cost_hurdle_multiple": 1.25,
                    "screen_slice_max_loss": 0.04,
                    "tail_weight": 0,
                    "max_tail_loss": None,
                    "recent_week": "warning_only_for_paper",
                },
                "validation": {
                    "net_return": 0.12813529385859979,
                    "sharpe": 2.657558216388672,
                    "max_drawdown_pct": -0.08794801382718277,
                    "trade_count": 222,
                },
                "positive_folds": 3,
                "fold_count": 4,
                "paired_incumbent_delta": {
                    "confidence": 0.9,
                    "estimate": 0.1504531018963993,
                    "lcb": -0.0365393254595867,
                    "p_value": 0.1277445109780439,
                    "paired_days": 120,
                    "t_stat": 1.0158941386375195,
                },
                "haircut": {
                    "cleared": False,
                    "expected_max_t": 2.6268,
                    "t_stat": 1.0158941386375195,
                    "trials": 132,
                },
                "warnings": ["audit-slice utility delta -0.0315 below floor -0.005"],
                "note": "User-approved post-hoc paper profile; production governance "
                "defaults and live-promotion authority are unchanged. "
                "Historical acceptance is not proof of forward edge.",
            },
            "provenance": {
                "evaluation_sdk": "5d465ec483f3d36ff64d27c832ed07665c52c60e",
                "script_sha256": "114c1b900360d31178d53de00959b498dd628f30b5ff5a383701b5eb094d095e",
                "implementation_ast_sha256": "41d1734d14f583a09ce3c7dc65e31f03984bde9b77d444a95cedc6c3e39e14f9",
                "strategy_params_sha256": "e25e5a9d65d91c4008e2d46f0e3e754f9314712ba06d128de6c9c6d8a62d59cb",
                "bars_sha256": "3bb7ce2badd89574fed6be18a2443507227d9f745b356b638edc97462dd5b558",
                "features_sha256": {
                    "funding": "e45b29054433012310aa4693813834216f8e6e5fc39d2cfcc0b175d94c301fa3"
                },
                "qualification_receipt_sha256": "d69e05307e9829f8f6bd23d8b9ef4fe3ca380edb1f514c6c7cd9691b3f42135c",
                "vitals_receipt_sha256": "353621104ddf81243ef83773ec8dfd422ca3545739c690bf8257d9822bc16988",
            },
        },
        strategy_inception_at="2026-09-04T00:00:00+00:00",
        cautions=(
            "Risk balancing improved the training walk-forward comparison, but July–September validation was weaker than the original; this is not uniform improvement.",
            "The final historical week had negative utility; it remains a paper-admission warning, not evidence of forward readiness.",
            "Historical qualification uses the named starter-paper-2026-09 profile at 1x. Inspect the leverage sweep before changing risk; account-halt monitors are not simulated.",
        ),
    ),
    StarterDefinition(
        id="diversified-momentum-taker-15m",
        selectable=False,
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
        selectable=False,
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
        selectable=False,
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
            "strategy_revision": "2.1.0",
            "source": "Verified Hydromancer/Hyperliquid candles and hourly funding; quiet archive "
            "intervals carry the previous close at zero volume without invented fills.",
            "window_start": "2025-09-27 01:00:00+00:00",
            "window_end": "2026-09-27 00:00:00+00:00",
            "evidence_role": "inspected_historical_development_not_sealed",
            "costs": {"taker_fee_bps_per_side": 4.5, "slippage_bps_per_side": 7.0},
            "funding_included": True,
            "funding": "Verified hourly settlements by signed exposure; no zero-fill of missing "
            "funding.",
            "fill_model": "Completed-bar decisions, next-bar fills, OHLC stops; passive orders "
            "require strict trade-through.",
            "return_after_costs_and_funding": 0.04502426433658124,
            "sharpe": 1.172650626871838,
            "max_drawdown": -0.017505793334040773,
            "jobs_v1_engine": {
                "return_after_fees_and_slippage": 0.04502426433658124,
                "sharpe": 1.172650626871838,
                "sortino": 0.5845276218193008,
                "max_drawdown": -0.017505793334040773,
                "trade_count": 188,
                "total_fees_usd": 217.15835412322355,
                "total_funding_usd": -4.513876876383198,
                "funding_included": True,
                "trace_valid": True,
            },
            "jobs_v1_leverage_sweep": {
                "leverage_semantics": "target_exposure",
                "account_halt_simulated": False,
                "qualification_scope": "default_1x_only",
                "results": STARTER_LEVERAGE_RESULTS["mixed-bollinger-pullback-1h"],
            },
            "qualification": {
                "profile": "starter-paper-2026-09",
                "quick_screen_passed": True,
                "full_development_passed": True,
                "paper_admission_passed": True,
                "reference": "cash",
                "leverage": 1,
                "forward_probation": "not_run",
                "live_promotion": "not_authorized",
                "policy_overrides": {
                    "full_dev_haircut_blocking": False,
                    "cost_hurdle_multiple": 1.25,
                    "screen_slice_max_loss": 0.04,
                    "tail_weight": 0,
                    "max_tail_loss": None,
                    "recent_week": "warning_only_for_paper",
                },
                "validation": {
                    "net_return": 0.002410172016045653,
                    "sharpe": 0.32247615890144987,
                    "max_drawdown_pct": -0.017504253883972958,
                    "trade_count": 54,
                },
                "positive_folds": 2,
                "fold_count": 4,
                "paired_incumbent_delta": {
                    "confidence": 0.9,
                    "estimate": 0.011798732853549375,
                    "lcb": -0.018380975352982158,
                    "p_value": 0.27944111776447106,
                    "paired_days": 120,
                    "t_stat": 0.6384126097166788,
                },
                "haircut": {
                    "cleared": False,
                    "expected_max_t": 2.5444,
                    "t_stat": 0.6384126097166788,
                    "trials": 104,
                },
                "warnings": [],
                "note": "User-approved post-hoc paper profile; production governance "
                "defaults and live-promotion authority are unchanged. "
                "Historical acceptance is not proof of forward edge.",
            },
            "provenance": {
                "evaluation_sdk": "5d465ec483f3d36ff64d27c832ed07665c52c60e",
                "script_sha256": "6fea5b56096207e5185a89e7a1d773c792cd0402202156343171850f203c8b3d",
                "implementation_ast_sha256": "dfa6a3e52939d4eaaf59dd82023643f35dca093dcf7c02959d94269c61033e16",
                "strategy_params_sha256": "0bad5cdc4527aaeee442b56eec92afbbd8fbaf91f6e37e03d1d2f4262b9706a8",
                "bars_sha256": "8d40668ffb3290c86d298b2cb522793726272d72f5e4c467b23568d6c7ab2586",
                "features_sha256": {
                    "funding": "422881945a2bc45f780447e069144aed6d503029714a54fccff038e549a1fa7d"
                },
                "qualification_receipt_sha256": "5a647c1385af717a9646be6f0ef54409d0047b05d934c56cab97be23d6e95ff4",
                "vitals_receipt_sha256": "ee600d77a08c3b464369b13041edb1c3e18b1c96b286951c0851f04063994296",
            },
        },
        cautions=(
            "The tested 36-hour holding revision failed broader validation; the qualified original 12-hour timeout is retained.",
            "Historical paper admission is not forward proof; the latest validation return was only 0.24% after costs and funding.",
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
            "Exit when RSI(7) rises above 60 or after 72 completed bars; target 25% per leg.",
        ),
        params={
            "rsi_period": 7,
            "entry_rsi": 20.0,
            "exit_rsi": 60.0,
            "trend_sma_period": 200,
            "volume_median_bars": 24,
            "volume_multiple": 1.0,
            "max_hold_bars": 72,
            "weight_per_leg": 0.25,
        },
        research_evidence={
            "strategy_revision": "2.1.0",
            "source": "Verified Hydromancer/Hyperliquid candles and hourly funding; quiet archive "
            "intervals carry the previous close at zero volume without invented fills.",
            "window_start": "2025-09-27 01:00:00+00:00",
            "window_end": "2026-09-27 00:00:00+00:00",
            "evidence_role": "inspected_historical_development_not_sealed",
            "costs": {"taker_fee_bps_per_side": 4.5, "slippage_bps_per_side": 7.0},
            "funding_included": True,
            "funding": "Verified hourly settlements by signed exposure; no zero-fill of missing "
            "funding.",
            "fill_model": "Completed-bar decisions, next-bar fills, OHLC stops; passive orders "
            "require strict trade-through.",
            "return_after_costs_and_funding": 0.1686610607790553,
            "sharpe": 2.58441898568479,
            "max_drawdown": -0.02238199380436111,
            "jobs_v1_engine": {
                "return_after_fees_and_slippage": 0.1686610607790553,
                "sharpe": 2.58441898568479,
                "sortino": 1.3502602516650113,
                "max_drawdown": -0.02238199380436111,
                "trade_count": 137,
                "total_fees_usd": 168.72149643556574,
                "total_funding_usd": -3.7508345120663904,
                "funding_included": True,
                "trace_valid": True,
            },
            "jobs_v1_leverage_sweep": {
                "leverage_semantics": "target_exposure",
                "account_halt_simulated": False,
                "qualification_scope": "default_1x_only",
                "results": STARTER_LEVERAGE_RESULTS["mixed-volume-capitulation-1h"],
            },
            "qualification": {
                "profile": "starter-paper-2026-09",
                "quick_screen_passed": True,
                "full_development_passed": True,
                "paper_admission_passed": True,
                "reference": "cash",
                "leverage": 1,
                "forward_probation": "not_run",
                "live_promotion": "not_authorized",
                "policy_overrides": {
                    "full_dev_haircut_blocking": False,
                    "cost_hurdle_multiple": 1.25,
                    "screen_slice_max_loss": 0.04,
                    "tail_weight": 0,
                    "max_tail_loss": None,
                    "recent_week": "warning_only_for_paper",
                },
                "validation": {
                    "net_return": 0.02088553509981983,
                    "sharpe": 1.7349199982530263,
                    "max_drawdown_pct": -0.01610989790139703,
                    "trade_count": 41,
                },
                "positive_folds": 4,
                "fold_count": 4,
                "paired_incumbent_delta": {
                    "confidence": 0.9,
                    "estimate": 0.06657236273287633,
                    "lcb": 0.03162671650023077,
                    "p_value": 0.001996007984031936,
                    "paired_days": 120,
                    "t_stat": 2.4285564248019167,
                },
                "haircut": {
                    "cleared": False,
                    "expected_max_t": 2.6268,
                    "t_stat": 2.4285564248019167,
                    "trials": 132,
                },
                "warnings": [],
                "note": "User-approved post-hoc paper profile; production governance "
                "defaults and live-promotion authority are unchanged. "
                "Historical acceptance is not proof of forward edge.",
            },
            "provenance": {
                "evaluation_sdk": "5d465ec483f3d36ff64d27c832ed07665c52c60e",
                "script_sha256": "54185f66f4680f07dcf8131308b2904f5fb205c092e08e178fa97cfefc7f3bb3",
                "implementation_ast_sha256": "da8b1f69ad045f9777b6997562addff83fba8055137ca7617768a024fcbfde88",
                "strategy_params_sha256": "da34299da39bc83078b9237951e76d1a00fad078a8b884d87fd2bd37209b4d88",
                "bars_sha256": "e0cc3ef03676fdca7821da62084acb90a64636ee45d790c2c88a273ef0be7e51",
                "features_sha256": {
                    "funding": "488f9c75ff879c9f5ad61bdb57b89734fb09185bf2117e248a534da636eedfd7"
                },
                "qualification_receipt_sha256": "c63c2e4fc1e1b4ba67cfcacdc207046cbca676be7113cb80156d30ea93a62491",
                "vitals_receipt_sha256": "7cc3aede77b2db5db514ccdf22fdbb712f482a7865f53fe985c00e3ce460cf5c",
            },
        },
        cautions=(
            "The RSI-60 revision improved all three training-fold metrics, but one of those three folds remained negative.",
            "Current cards include verified funding and 7 bps taker slippage. Historical paper qualification is not proof of forward edge.",
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
            "Allocate 50% to HYPE and 25% to each equity perp; exit above RSI 60 or after 72 hours, with a fill-relative catastrophe stop and 24-hour stop cooldown.",
        ),
        params={
            "rsi_period": 7,
            "entry_rsi": 20.0,
            "exit_rsi": 60.0,
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
            "strategy_revision": "2.1.0",
            "source": "Verified Hydromancer/Hyperliquid candles and hourly funding; quiet archive "
            "intervals carry the previous close at zero volume without invented fills.",
            "window_start": "2025-09-27 01:00:00+00:00",
            "window_end": "2026-09-27 00:00:00+00:00",
            "evidence_role": "inspected_historical_development_not_sealed",
            "costs": {
                "taker_fee_bps_per_side": 4.5,
                "slippage_bps_per_side": 7.0,
                "maker_fee_bps": 1.5,
                "maker_trade_through_bps": 1.0,
            },
            "funding_included": True,
            "funding": "Verified hourly settlements by signed exposure; no zero-fill of missing "
            "funding.",
            "fill_model": "Completed-bar decisions, next-bar fills, OHLC stops; passive orders "
            "require strict trade-through.",
            "return_after_costs_and_funding": 0.26473681349337497,
            "sharpe": 2.7457660959669457,
            "max_drawdown": -0.04024753555332018,
            "jobs_v1_engine": {
                "return_after_fees_and_slippage": 0.26473681349337497,
                "sharpe": 2.7457660959669457,
                "sortino": 1.295780564200395,
                "max_drawdown": -0.04024753555332018,
                "trade_count": 101,
                "total_fees_usd": 102.8451534664202,
                "total_funding_usd": -4.033294184985762,
                "funding_included": True,
                "trace_valid": True,
            },
            "jobs_v1_leverage_sweep": {
                "leverage_semantics": "target_exposure",
                "account_halt_simulated": False,
                "qualification_scope": "default_1x_only",
                "results": STARTER_LEVERAGE_RESULTS["balanced-passive-capitulation-1h"],
            },
            "qualification": {
                "profile": "starter-paper-2026-09",
                "quick_screen_passed": True,
                "full_development_passed": True,
                "paper_admission_passed": True,
                "reference": "cash",
                "leverage": 1,
                "forward_probation": "not_run",
                "live_promotion": "not_authorized",
                "policy_overrides": {
                    "full_dev_haircut_blocking": False,
                    "cost_hurdle_multiple": 1.25,
                    "screen_slice_max_loss": 0.04,
                    "tail_weight": 0,
                    "max_tail_loss": None,
                    "recent_week": "warning_only_for_paper",
                },
                "validation": {
                    "net_return": 0.02034200182791901,
                    "sharpe": 1.3945697393125154,
                    "max_drawdown_pct": -0.019558930717481115,
                    "trade_count": 29,
                },
                "positive_folds": 3,
                "fold_count": 4,
                "paired_incumbent_delta": {
                    "confidence": 0.9,
                    "estimate": 0.06937743332118514,
                    "lcb": 0.016754429557028154,
                    "p_value": 0.023952095808383235,
                    "paired_days": 120,
                    "t_stat": 1.7420595661876024,
                },
                "haircut": {
                    "cleared": False,
                    "expected_max_t": 2.6268,
                    "t_stat": 1.7420595661876024,
                    "trials": 132,
                },
                "warnings": [],
                "note": "User-approved post-hoc paper profile; production governance "
                "defaults and live-promotion authority are unchanged. "
                "Historical acceptance is not proof of forward edge.",
            },
            "provenance": {
                "evaluation_sdk": "5d465ec483f3d36ff64d27c832ed07665c52c60e",
                "script_sha256": "54185f66f4680f07dcf8131308b2904f5fb205c092e08e178fa97cfefc7f3bb3",
                "implementation_ast_sha256": "da8b1f69ad045f9777b6997562addff83fba8055137ca7617768a024fcbfde88",
                "strategy_params_sha256": "7e11a05320968aed339b23b2b5b12653c52c098dc67268a1495c64b5989a7fb3",
                "bars_sha256": "a00693eb8a2af63a6b5acbf4f4a250b9c91f8b9c42d4d224e567e3f20d607061",
                "features_sha256": {
                    "funding": "f1b9c2a7080e6ba236531c2689448981aef4a7436029ee63a0e8766b15b62638"
                },
                "qualification_receipt_sha256": "b226663ae921bafaad7b0a84df76f6f883cf28e6df667da36d68e341fc625eb1",
                "vitals_receipt_sha256": "fffddaf5a0321f6c64c1e8cc7f14e9c800931fb2d8ec0746244a21e4a7c55d31",
            },
        },
        cautions=(
            "The RSI-60 revision improved all three training-fold metrics and passed historical paper admission, but its full-year drawdown rose to 4.02% and Sharpe fell to 2.75 versus the RSI-50 original.",
            "Candle trade-through is conservative about touch fills but cannot reproduce exact queue position or partial fills.",
            "Live ALO routing is intentionally disabled until durable venue fill/cancel reconciliation is available.",
        ),
    ),
    StarterDefinition(
        id="mixed-momentum-rank-1h",
        selectable=False,
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
        selectable=False,
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
            "strategy_revision": "2.1.0",
            "source": "Verified Hydromancer/Hyperliquid candles and hourly funding; quiet archive "
            "intervals carry the previous close at zero volume without invented fills.",
            "window_start": "2025-09-27 00:15:00+00:00",
            "window_end": "2026-09-27 00:00:00+00:00",
            "evidence_role": "inspected_historical_development_not_sealed",
            "costs": {"taker_fee_bps_per_side": 4.5, "slippage_bps_per_side": 7.0},
            "funding_included": True,
            "funding": "Verified hourly settlements by signed exposure; no zero-fill of missing "
            "funding.",
            "fill_model": "Completed-bar decisions, next-bar fills, OHLC stops; passive orders "
            "require strict trade-through.",
            "return_after_costs_and_funding": 0.2935306808312228,
            "sharpe": 1.2684932482151052,
            "max_drawdown": -0.15470199107563368,
            "jobs_v1_engine": {
                "return_after_fees_and_slippage": 0.2935306808312228,
                "sharpe": 1.2684932482151052,
                "sortino": 1.4845286903146269,
                "max_drawdown": -0.15470199107563368,
                "trade_count": 135,
                "total_fees_usd": 158.6069031200341,
                "total_funding_usd": -64.03557800062421,
                "funding_included": True,
                "trace_valid": True,
            },
            "jobs_v1_leverage_sweep": {
                "leverage_semantics": "target_exposure",
                "account_halt_simulated": False,
                "qualification_scope": "default_1x_only",
                "results": STARTER_LEVERAGE_RESULTS["mixed-sleeve-momentum-15m"],
            },
            "qualification": {
                "profile": "starter-paper-2026-09",
                "quick_screen_passed": True,
                "full_development_passed": True,
                "paper_admission_passed": True,
                "reference": "cash",
                "leverage": 1,
                "forward_probation": "not_run",
                "live_promotion": "not_authorized",
                "policy_overrides": {
                    "full_dev_haircut_blocking": False,
                    "cost_hurdle_multiple": 1.25,
                    "screen_slice_max_loss": 0.04,
                    "tail_weight": 0,
                    "max_tail_loss": None,
                    "recent_week": "warning_only_for_paper",
                },
                "validation": {
                    "net_return": 0.08074180491550242,
                    "sharpe": 1.8258551483101348,
                    "max_drawdown_pct": -0.06847645849609292,
                    "trade_count": 31,
                },
                "positive_folds": 3,
                "fold_count": 4,
                "paired_incumbent_delta": {
                    "confidence": 0.9,
                    "estimate": 0.22482105909695152,
                    "lcb": 0.08403293676551428,
                    "p_value": 0.021956087824351298,
                    "paired_days": 120,
                    "t_stat": 1.8658340882699591,
                },
                "haircut": {
                    "cleared": False,
                    "expected_max_t": 2.5444,
                    "t_stat": 1.8658340882699591,
                    "trials": 104,
                },
                "warnings": ["audit-slice utility delta -0.0122 below floor -0.005"],
                "note": "User-approved post-hoc paper profile; production governance "
                "defaults and live-promotion authority are unchanged. "
                "Historical acceptance is not proof of forward edge.",
            },
            "provenance": {
                "evaluation_sdk": "5d465ec483f3d36ff64d27c832ed07665c52c60e",
                "script_sha256": "4c0e5b15c92e1b3202102fa701c1650dcbf0a15265d18b553b0c1df018edb8d5",
                "implementation_ast_sha256": "e954b0b1877eada4231aefeb444c4706d86087b3de29ffaea14b79d43b0c9996",
                "strategy_params_sha256": "506edcc21a4568629dd7f66b659b0a1a94de9cb27f1572e65f55d4c35acb02f2",
                "bars_sha256": "dd10eed2808941b5add760ca1d48fc38634ef03cb7bc2c69b87174a11fad8b7a",
                "features_sha256": {
                    "funding": "20645e91fdf93c5ce96d4978438b3d5a596619f1e40d659c0b38fd94004a4bf4"
                },
                "qualification_receipt_sha256": "81f98d388412a7164b902f37d8256a942751840309f10c8a1c34edac23519185",
                "vitals_receipt_sha256": "7e2542ae74f0c3609dd151a5606ce1885d8051cbdbf00c38f28e9b587f0c6b0a",
            },
        },
        cautions=("One of four chronological folds was negative.",),
    ),
    StarterDefinition(
        id="mixed-low-vol-rank-15m",
        selectable=False,
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
        selectable=False,
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
        selectable=False,
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
        selectable=False,
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
        selectable=False,
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
        id="diversified-liquidation-flush-maker-15m",
        selectable=False,
        name="Diversified Liquidation Flush Maker · 15m",
        family="liquidation_flush",
        summary=(
            "Rests post-only orders against a one-day move that open interest "
            "did not survive, buying long liquidations and selling short "
            "squeezes for a few hours, across nineteen Hyperliquid perps."
        ),
        timeframe="15m",
        module="wayfinder_paths.jobs.strategies.mixed_liquidation_flush",
        symbols=_FUNDING_OI_DIVERGENCE_SYMBOLS,
        crypto_assets=_FUNDING_OI_DIVERGENCE_SYMBOLS,
        tokenized_equities=(),
        rules=(
            "A flush is a trailing 24-hour move of at least 8% while open interest fell at least 10% over the same 24 hours: positions were force-closed into the move, not added.",
            "Fade it: buy a long liquidation flush, sell a short squeeze flush, but never while the latest bar still spans more than 2 ATR(24): a cascade in progress is not a finished flush.",
            "Rest a post-only order 0.5 ATR(24) beyond the close, replaced every bar; ride while the flush condition persists and exit with a marketable order 12 completed bars after it ends, or immediately if it flips side.",
            "Size every leg at 5% of equity; the catastrophe stop is 12x ATR(24) bounded to 30–50%.",
        ),
        params={
            "flush_return_bars": 96,
            "flush_return_min": 0.08,
            "flush_oi_bars": 96,
            "flush_oi_drop_min": 0.10,
            "sides": "both",
            "hold_after_signal_bars": 12,
            "weight_per_leg": 0.05,
            "entry_order_type": "maker",
            "entry_offset_atr": 0.5,
            "entry_ttl_bars": 1,
            "entry_max_bar_range_atr": 2.0,
            "maker_fee_bps": 1.5,
            "maker_trade_through_bps": 1.0,
            "stop_atr_period": 24,
            "stop_atr_multiple": 12.0,
            "stop_min_pct": 0.30,
            "stop_max_pct": 0.50,
        },
        research_evidence={
            **_FUNDING_OI_DIVERGENCE_RESEARCH_METHOD,
            "source": (
                "Hydromancer Reservoir 1-second Hyperliquid candles aggregated to "
                "15m and daily open interest aggregated from a Hyperliquid "
                "account-snapshot archive"
            ),
            "tradeable_from": "2025-08-02T00:00:00+00:00",
            "validation": (
                "open-interest indicators (flush, exhaustion, build divergence, "
                "unwind, open-interest-confirmed momentum) were screened on 19 "
                "Hyperliquid perps with a next-bar taker model, then re-simulated "
                "per symbol in the jobs_v1 engine and pooled (cross_asset_lift); "
                "only the flush kept both halves and 11 or more symbols positive; "
                "all slices were reviewed before publication, so forward paper "
                "results remain the real holdout"
            ),
            "strategy_family": (
                "open-interest liquidation flush fade, passive entries"
            ),
            "sharpe": 1.9476,
            "max_drawdown": -0.0432,
            "chronological_fold_method": (
                "fixed-parameter continuous jobs_v1 path divided into four "
                "contiguous quarters"
            ),
            "chronological_fold_returns": [0.0465, 0.0318, 0.0441, 0.023],
            "cross_asset_lift": {
                "method": (
                    "jobs_v1 engine per symbol on cached frames, daily returns "
                    "pooled at equal weight, 2025-08-31 to 2026-09-04, stop "
                    "overlay not binding"
                ),
                "pooled_sharpe": 2.93,
                "pooled_return": 0.1633,
                "halves_sharpe": [3.24, 2.62],
                "symbols_positive": "16 of 19",
                "without_cascade_guard": {
                    "pooled_sharpe": 0.87,
                    "halves_sharpe": [0.26, 2.84],
                    "symbols_positive": "12 of 19",
                    "note": (
                        "the difference is the 2025-10-10 cascade: four ungated "
                        "entries were stopped through wicks that recovered; the "
                        "guard at 3 ATR gives the same result as 2 ATR"
                    ),
                },
                "taker_pooled_sharpe_ungated": 0.74,
                "long_only_taker_pooled_sharpe_ungated": 0.41,
                "open_interest_exhaustion_fade_pooled_sharpe": -1.9,
                "open_interest_confirmed_momentum_lab_sharpe": 1.19,
            },
            "signal_screen": {
                "hyperliquid_pooled_sharpe": 2.16,
                "hyperliquid_quarters": [0.012, 0.041, 0.035, 0.041],
                "threshold_grid": (
                    "moves of 5-8% with open-interest drops of 8-15% and holds "
                    "of 3-12 hours all pooled between 0.8 and 2.2; holds beyond "
                    "18 hours lost the edge"
                ),
                "note": (
                    "the taker screen holds through candle wicks; the engine "
                    "figures above include the 2025-10-10 cascade, where two "
                    "symbols wicked through a 60% diagnostic stop"
                ),
            },
            "funding_pnl_note": (
                "with hourly funding P&L included the same path returned 15.45% "
                "at Sharpe 1.96 (drawdown -4.31%): holds are too short for "
                "funding to matter"
            ),
            "jobs_v1_engine": {
                "return_after_fees_and_slippage": 0.1534,
                "sharpe": 1.9476,
                "max_drawdown": -0.0432,
                "trade_count": 602,
                "maker_fills": 302,
                "total_fees_usd": 96.92,
                "stop_count": 1,
                "full_period_vs_no_stop": "regressed",
                "no_stop_return": 0.1554,
                "no_stop_return_delta": -0.002,
                "no_stop_note": (
                    "the one stop is the 2025-10-10 cascade: VVV was bought five "
                    "hours before the 21:30 UTC wick, which cleared every "
                    "catastrophe floor from 30% to 40%; without the stop that "
                    "position recovered, so the first quarter reads 4.65% with "
                    "the stop against 4.84% without"
                ),
                "chronological_folds_non_regressing": 3,
                "funding_included": False,
                "trace_valid": True,
            },
        },
        strategy_inception_at="2026-09-05T00:00:00+00:00",
        cautions=(
            "Open interest has no public history on Hyperliquid: a new job records it at every wake from its first day, and the strategy stands down until a full day of open-interest history exists. The backtest used a daily archive of account snapshots, so the live signal updates with the recorded cadence rather than daily stamps.",
            "The flush cannot tell a finished cascade from one still under way: the 2 ATR bar-range guard skipped the 2025-10-10 entries that were stopped without it, but a VVV position bought five hours before that evening's wick was still stopped at -30%. The catastrophe stop (30% floor) is the only per-position guard and a 5% leg can lose 1.5-2.5% of equity before it triggers.",
            "Only 1x to 4x stayed within the -20% account-halt threshold in the leverage sweep.",
            "Flushes are rare (a few per symbol per year), so the evidence rests on a small number of events and one year of Hyperliquid history; no cross-venue check was possible because other venues publish only a month of open-interest history.",
            "Funding P&L is excluded from the headline figures.",
            "Maker fills use the strict candle trade-through model; live limit routing stays disabled until durable venue fill/cancel reconciliation lands.",
        ),
        features=(
            {
                "name": "open_interest",
                "max_age_seconds": 172_800,
                "stale_policy": "skip",
            },
        ),
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
    return [
        definition.to_dict()
        for definition in STARTER_DEFINITIONS
        if definition.selectable
    ]


def get_starter(starter_id: str) -> StarterDefinition:
    """Resolve a stable ID, including retired definitions for existing jobs/research."""
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
        recorded_evidence = store.read_json(
            existing.id, "results/backtest/starter_evidence.json", default=None
        )
        starter_evidence = (
            recorded_evidence
            if isinstance(recorded_evidence, dict)
            and recorded_evidence.get("id") == definition.id
            else definition.to_dict()
        )
        return {
            "created": False,
            "job": existing.to_dict(),
            "job_yaml": str(job_path),
            "script_entrypoint": str(entrypoint) if entrypoint is not None else None,
            "starter": starter_evidence,
            "selected_leverage": selected_leverage,
            "leverage_warning": leverage_warning,
        }

    if not definition.selectable:
        raise ValueError(
            f"Starter {definition.id!r} is retired from new selection because it "
            "has not qualified under the current catalogue requirements. "
            "Existing jobs are unchanged; choose a starter from the current catalogue."
        )

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
    job.execution_spec = harnessed_execution_spec(
        list(definition.symbols),
        definition.timeframe,
        features=definition.features or None,
        robustness_plan=STARTER_ROBUSTNESS_PLANS.get(definition.id),
    )
    job.execution_params = {
        **configured_params,
        **harnessed_execution_params(
            list(definition.symbols),
            lookback_bars=starter_lookback_bars(definition),
            leverage=selected_leverage,
        ),
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
