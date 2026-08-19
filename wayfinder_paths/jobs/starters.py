"""Selectable, paper-first jobs_v1 starter strategies.

The catalog owns exact rules and research provenance. Selecting a starter
creates a normal Wayfinder job; from that point, the standard backtest and
forward-result machinery owns the user's results.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.models import WayfinderJob, safe_job_id
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.strategies._starter_utils import (
    MEAN_REVERSION_STOP_DEFAULTS,
    PAIR_PROTECTION_DEFAULTS,
    RANKING_STOP_DEFAULTS,
)

STARTER_CATALOG_VERSION = "1.3.0"
STARTER_STRATEGY_INCEPTION_AT = "2026-08-18T00:00:00+00:00"
STARTER_EVIDENCE_REVISION = "1.3.0"


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
    cautions: tuple[str, ...] = ()

    def configured_params(self) -> dict[str, Any]:
        if self.family == "mean_reversion":
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
        return {
            "max_drawdown": -0.06 if self.family == "mean_reversion" else -0.20,
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
        payload["params"] = self.configured_params()
        payload["risk_limits"] = self.risk_limits()
        payload["risk_controls"] = self.risk_controls()
        payload["research_evidence"] = {
            **payload["research_evidence"],
            "strategy_revision": STARTER_EVIDENCE_REVISION,
            "risk_overlay_backtest_status": "validated",
            "risk_overlay_backtest_scope": "per_position_ohlc_stops",
            "risk_overlay_note": (
                "The jobs_v1 engine figures include the 1.3.0 per-position stop "
                "overlay. Live pair-group and account monitors run between strategy "
                "bars and are not included in these historical figures."
            ),
        }
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
                "strategy_inception_at": STARTER_STRATEGY_INCEPTION_AT,
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


STARTER_DEFINITIONS: tuple[StarterDefinition, ...] = (
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
)


def starter_catalog() -> list[dict[str, Any]]:
    return [definition.to_dict() for definition in STARTER_DEFINITIONS]


def get_starter(starter_id: str) -> StarterDefinition:
    normalized = str(starter_id).strip().lower()
    for definition in STARTER_DEFINITIONS:
        if definition.id == normalized:
            return definition
    raise KeyError(f"unknown starter strategy: {starter_id}")


def create_starter_job(
    starter_id: str,
    *,
    job_id: str | None = None,
    store: JobStore | None = None,
    compile_job: bool = True,
    initializer_session_id: str | None = None,
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
        return {
            "created": False,
            "job": existing.to_dict(),
            "job_yaml": str(job_path),
            "script_entrypoint": str(entrypoint) if entrypoint is not None else None,
            "starter": definition.to_dict(),
        }

    from wayfinder_paths.jobs.execution.primitives import bar_interval_seconds

    configured_params = definition.configured_params()
    interval_seconds = int(bar_interval_seconds(definition.timeframe) or 0)
    if interval_seconds <= 0:
        raise ValueError(f"unsupported starter timeframe: {definition.timeframe}")
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
        agent_mode="monitor",
        agent_wake_seconds=3600,
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
        },
        "validation": {"mode": "strict", "require_scenarios": False},
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
    }
    job.controller["starter"] = {
        "id": definition.id,
        "catalog_version": STARTER_CATALOG_VERSION,
        "strategy_inception_at": STARTER_STRATEGY_INCEPTION_AT,
        "job_tracking_inception_at": job.created_at,
        "paper_only": True,
        "risk_limits": definition.risk_limits(),
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
    store.write_json(job.id, "results/backtest/starter_evidence.json", evidence)

    result: dict[str, Any] = {
        "created": True,
        "job": job.to_dict(),
        "job_yaml": str(job_path),
        "script_entrypoint": str(entrypoint),
        "starter": definition.to_dict(),
    }
    if compile_job:
        from wayfinder_paths.jobs.compiler import JobCompiler
        from wayfinder_paths.jobs.sync import sync_all_jobs

        result["compile"] = JobCompiler(store=store).compile(job)
        sync_all_jobs(store=store)
    return result
