"""Economic promotion evidence: objective vectors, the owner utility, and the
paired candidate-vs-incumbent fold evaluation behind `economic_ready`.

Design constraints this module encodes:
- The gate replays BOTH param/code sets on the same outer OOS folds with the
  same bars and costs — inner parameter selection already happened during
  development, so no search runs here (K folds x 2 sides of test-window sims).
- The confidence bound is on the GROWTH delta via a moving-block bootstrap of
  paired daily log-return deltas; risk terms (downside, tail, fees) enter the
  utility as point estimates. Conservative and small-sample-sane: at 24-160
  trades a full multivariate bootstrap would be theater.
- The terminal audit slice is excluded from the fold layout and evaluated
  once, by this code, at decision time. Agents can technically backtest over
  those bars during development, so this is freshness pressure rather than a
  cryptographic seal — the honest 80% of a sealed-audit service.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.execution.primitives import ExecutionSpec
from wayfinder_paths.jobs.execution.simulator import (
    PreparedExecutionDataset,
    simulate_execution,
)
from wayfinder_paths.jobs.execution.walk_forward import _slice
from wayfinder_paths.jobs.regime import (
    PORTFOLIO_REGIME_CLASSIFIER,
    REGIME_FEATURE_WARMUP_BARS,
    classify_portfolio_regimes,
    enabled_regimes,
    partition_regime_returns,
    regime_universe,
    utc_timestamp,
)


def objective_vector(
    equity_curve: Sequence[Mapping[str, Any]],
    trades: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return/risk quantities the owner utility is defined over. Downside
    deviation (not plain variance) so upside volatility is not penalized."""
    daily = daily_log_returns(equity_curve)
    returns = [value for _, value in daily]
    growth = sum(returns)
    negatives = [value for value in returns if value < 0]
    downside = (
        math.sqrt(sum(value * value for value in negatives) / len(returns))
        if returns
        else 0.0
    )
    base_equity = float(equity_curve[0]["equity"]) if equity_curve else 0.0
    # Engine fill rows carry realized_pnl_delta (openers ~-fee, closers the
    # realized result); forward rows carry net_pnl. Reading only "pnl" made
    # tail_loss silently 0 — the tail ceiling could never trip.
    closes_only = [row for row in trades if row.get("reduce_only") is True]
    counted = closes_only if closes_only else trades
    pnls = []
    for row in counted:
        value: Any = row.get("pnl")
        if value is None:
            value = row.get("net_pnl")
        if value is None:
            value = row.get("realized_pnl_delta")
        pnls.append(float(value or 0.0))
    worst_k = max(1, len(pnls) // 10)
    tail_loss = (
        abs(sum(sorted(pnls)[:worst_k])) / base_equity if base_equity > 0 else 0.0
    )
    fees = sum(float(row.get("fee") or 0.0) for row in trades)
    fee_load = fees / base_equity if base_equity > 0 else 0.0
    max_dd = _max_drawdown(equity_curve)
    return {
        "net_log_growth": growth,
        "downside_deviation": downside,
        "tail_loss": tail_loss,
        "fee_load": fee_load,
        "max_drawdown_pct": max_dd,
        "trade_count": len(counted),
        "day_count": len(returns),
    }


def utility(vector: Mapping[str, Any], weights: Mapping[str, Any]) -> float:
    return (
        float(vector["net_log_growth"])
        - float(weights.get("downside", 0.0)) * float(vector["downside_deviation"])
        - float(weights.get("tail", 0.0)) * float(vector["tail_loss"])
        - float(weights.get("turnover", 0.0)) * float(vector["fee_load"])
    )


def daily_log_returns(
    equity_curve: Sequence[Mapping[str, Any]],
) -> list[tuple[str, float]]:
    """Calendar-day log returns from an equity curve (last equity per day)."""
    by_day: dict[str, float] = {}
    for row in equity_curve:
        day = str(pd.Timestamp(row["timestamp"]).date())
        by_day[day] = float(row["equity"])
    days = sorted(by_day)
    returns: list[tuple[str, float]] = []
    for prev, cur in zip(days, days[1:], strict=False):
        if by_day[prev] > 0 and by_day[cur] > 0:
            returns.append((cur, math.log(by_day[cur] / by_day[prev])))
    return returns


def paired_daily_deltas(
    baseline: list[tuple[str, float]],
    candidate: list[tuple[str, float]],
) -> list[float]:
    """Candidate-minus-baseline log return on the intersection of days —
    paired so common market moves cancel out of the delta."""
    base_map = dict(baseline)
    return [value - base_map[day] for day, value in candidate if day in base_map]


def regime_conditioned_objective(
    equity_curve: Sequence[Mapping[str, Any]],
    trades: Sequence[Mapping[str, Any]],
    *,
    labels: Mapping[pd.Timestamp, str],
    target_regimes: Sequence[str],
    weights: Mapping[str, Any],
) -> dict[str, Any]:
    """Marked-equity objective partitioned by the regime of each return bar.

    Positions held through a flip contribute to the new cell immediately;
    attributing a whole trade to entry or exit would hide exactly the regime
    transition loss this contract is intended to bound.
    """
    partition = partition_regime_returns(
        equity_curve,
        trades,
        labels=labels,
        target_regimes=target_regimes,
    )
    target_daily = list(partition["target_daily"])
    outside_daily = list(partition["outside_daily"])
    target_returns = [value for _, value in target_daily]
    outside_returns = [value for _, value in outside_daily]
    target_vector = _conditional_vector(
        target_returns, int(partition["target_trade_count"])
    )
    outside_vector = _conditional_vector(
        outside_returns, int(partition["outside_trade_count"])
    )
    return {
        "target": target_vector,
        "outside": {
            **outside_vector,
            "loss_pct": max(
                0.0, 1.0 - math.exp(float(outside_vector["net_log_growth"]))
            ),
        },
        "target_daily": target_daily,
        "outside_daily": outside_daily,
        "target_utility": utility(target_vector, weights),
        "outside_utility": utility(outside_vector, weights),
    }


def _conditional_vector(returns: Sequence[float], trade_count: int) -> dict[str, Any]:
    negatives = [value for value in returns if value < 0]
    downside = (
        math.sqrt(sum(value * value for value in negatives) / len(returns))
        if returns
        else 0.0
    )
    # Tail/fee allocation remains in the whole-book hard constraints.  These
    # fields keep the owner utility shape identical without pretending a
    # closing trade's entire tail loss belongs to one intrabar market cell.
    return {
        "net_log_growth": sum(returns),
        "downside_deviation": downside,
        "tail_loss": 0.0,
        "fee_load": 0.0,
        "max_drawdown_pct": _max_drawdown_from_returns(returns),
        "trade_count": int(trade_count),
        "day_count": len(returns),
    }


def _max_drawdown_from_returns(returns: Sequence[float]) -> float:
    value = 1.0
    peak = 1.0
    worst = 0.0
    for change in returns:
        value *= math.exp(float(change))
        peak = max(peak, value)
        worst = max(worst, (peak - value) / peak)
    return worst


def block_bootstrap_lcb(
    deltas: list[float],
    *,
    block_len: int,
    iterations: int,
    confidence: float,
    seed: int = 7,
) -> float | None:
    """Lower confidence bound on the TOTAL growth delta via a circular
    moving-block bootstrap. None when there is nothing to resample."""
    totals = _block_bootstrap_totals(
        deltas, block_len=block_len, iterations=iterations, seed=seed
    )
    if not totals:
        return None
    index = int((1.0 - confidence) * (len(totals) - 1))
    return totals[index]


def block_bootstrap_p_value(
    deltas: list[float],
    *,
    block_len: int,
    iterations: int,
    seed: int = 7,
) -> float | None:
    """One-sided probability that the paired total is nonpositive."""
    totals = _block_bootstrap_totals(
        deltas, block_len=block_len, iterations=iterations, seed=seed
    )
    if not totals:
        return None
    return (1 + sum(total <= 0 for total in totals)) / (len(totals) + 1)


def _block_bootstrap_totals(
    deltas: list[float], *, block_len: int, iterations: int, seed: int
) -> list[float]:
    n = len(deltas)
    if n < 2:
        return []
    block = max(1, min(block_len, n))
    rng = random.Random(seed)
    blocks_needed = math.ceil(n / block)
    totals: list[float] = []
    for _ in range(iterations):
        sample: list[float] = []
        for _ in range(blocks_needed):
            start = rng.randrange(n)
            sample.extend(deltas[(start + offset) % n] for offset in range(block))
        totals.append(sum(sample[:n]))
    totals.sort()
    return totals


def paired_fold_evaluation(
    *,
    baseline_script: str | Path | Callable[..., Any],
    candidate_script: str | Path | Callable[..., Any],
    dataset: PreparedExecutionDataset,
    spec: ExecutionSpec,
    baseline_params: Mapping[str, Any],
    candidate_params: Mapping[str, Any],
    constitution: Mapping[str, Any],
    warmup_bars: int = 60,
) -> dict[str, Any]:
    """Replay both sides on identical outer OOS folds + the terminal audit
    slice; return the paired evidence the economic gate decides on."""
    evaluation = constitution["evaluation"]
    weights = constitution["objective"]["weights"]
    target_regimes = enabled_regimes(candidate_params)
    effective_warmup = max(
        int(warmup_bars), REGIME_FEATURE_WARMUP_BARS if target_regimes else 0
    )
    regime_labels: dict[pd.Timestamp, str] = {}
    if target_regimes:
        classified = classify_portfolio_regimes(
            dataset.bars.to_frame(),
            universe=regime_universe(candidate_params, dataset.bars.symbols),
        )
        regime_labels = {
            utc_timestamp(stamp): str(label) for stamp, label in classified.items()
        }
    folds = int(evaluation["folds"])
    timestamps = dataset.bars.timestamps
    total = len(timestamps)

    bar_seconds = _bar_seconds(spec)
    audit_bars = max(1, int(evaluation["audit_days"] * 86_400 // bar_seconds))
    dev_total = total - audit_bars
    # Fold the most recent THIRD of development history: recent regime is what
    # promotion risks money on, and bounded test windows keep this affordable.
    test_bars = max(1, min(dev_total // (folds * 3), dev_total // folds))
    min_history = effective_warmup + folds * test_bars
    if dev_total <= min_history or test_bars < 8:
        return {
            "status": "insufficient_history",
            "detail": (
                f"{total} bars with audit_bars={audit_bars} cannot fit "
                f"{folds} folds (needs > {min_history + audit_bars})"
            ),
        }

    fold_rows: list[dict[str, Any]] = []
    baseline_daily: list[tuple[str, float]] = []
    candidate_daily: list[tuple[str, float]] = []
    baseline_pool: dict[str, list[Any]] = defaultdict(list)
    candidate_pool: dict[str, list[Any]] = defaultdict(list)
    regime_pools: dict[str, dict[str, Any]] = {
        side: {
            "target_daily": [],
            "outside_daily": [],
            "target_entries": 0,
            "outside_entries": 0,
        }
        for side in ("baseline", "candidate")
    }
    for index in range(folds):
        test_end = dev_total - (folds - 1 - index) * test_bars
        test_start = test_end - test_bars
        side_rows: dict[str, dict[str, Any]] = {}
        side_regime: dict[str, dict[str, Any]] = {}
        for side, script, params in (
            ("baseline", baseline_script, baseline_params),
            ("candidate", candidate_script, candidate_params),
        ):
            equity, trades = _oos_window(
                script,
                dataset,
                spec,
                params,
                timestamps,
                test_start,
                test_end,
                effective_warmup,
            )
            vector = objective_vector(equity, trades)
            side_rows[side] = vector
            pool = baseline_pool if side == "baseline" else candidate_pool
            pool["equity"].extend(equity)
            pool["trades"].extend(trades)
            daily = daily_log_returns(equity)
            (baseline_daily if side == "baseline" else candidate_daily).extend(daily)
            if target_regimes:
                conditioned = regime_conditioned_objective(
                    equity,
                    trades,
                    labels=regime_labels,
                    target_regimes=target_regimes,
                    weights=weights,
                )
                side_regime[side] = {
                    key: value
                    for key, value in conditioned.items()
                    if not key.endswith("_daily")
                }
                regime_pools[side]["target_daily"].extend(conditioned["target_daily"])
                regime_pools[side]["outside_daily"].extend(conditioned["outside_daily"])
                regime_pools[side]["target_entries"] = int(
                    regime_pools[side]["target_entries"]
                ) + int(conditioned["target"]["trade_count"])
                regime_pools[side]["outside_entries"] = int(
                    regime_pools[side]["outside_entries"]
                ) + int(conditioned["outside"]["trade_count"])
        overall_delta = utility(side_rows["candidate"], weights) - utility(
            side_rows["baseline"], weights
        )
        decision_delta = (
            float(side_regime["candidate"]["target_utility"])
            - float(side_regime["baseline"]["target_utility"])
            if target_regimes
            else overall_delta
        )
        fold_rows.append(
            {
                "fold": index,
                "test": {
                    "start": str(timestamps[test_start]),
                    "end": str(timestamps[test_end - 1]),
                    "bars": test_bars,
                },
                "baseline": side_rows["baseline"],
                "candidate": side_rows["candidate"],
                "delta_utility": decision_delta,
                "overall_delta_utility": overall_delta,
                **({"regime": side_regime} if target_regimes else {}),
            }
        )

    baseline_vector = objective_vector(baseline_pool["equity"], baseline_pool["trades"])
    candidate_vector = objective_vector(
        candidate_pool["equity"], candidate_pool["trades"]
    )
    deltas = paired_daily_deltas(baseline_daily, candidate_daily)
    growth_lcb = block_bootstrap_lcb(
        deltas,
        block_len=int(evaluation["block_days"]),
        iterations=int(evaluation["bootstrap_iterations"]),
        confidence=float(evaluation["confidence"]),
    )
    growth_p_value = block_bootstrap_p_value(
        deltas,
        block_len=int(evaluation["block_days"]),
        iterations=int(evaluation["bootstrap_iterations"]),
    )
    delta_estimate = utility(candidate_vector, weights) - utility(
        baseline_vector, weights
    )
    # Conservative composite: growth term at its LCB, risk terms at their
    # point estimates. See module docstring for why.
    delta_lcb = None
    if growth_lcb is not None:
        risk_delta = delta_estimate - (
            float(candidate_vector["net_log_growth"])
            - float(baseline_vector["net_log_growth"])
        )
        delta_lcb = growth_lcb + risk_delta

    audit_rows: dict[str, dict[str, Any]] = {}
    audit_regime: dict[str, dict[str, Any]] = {}
    for side, script, params in (
        ("baseline", baseline_script, baseline_params),
        ("candidate", candidate_script, candidate_params),
    ):
        equity, trades = _oos_window(
            script,
            dataset,
            spec,
            params,
            timestamps,
            dev_total,
            total,
            effective_warmup,
        )
        audit_rows[side] = objective_vector(equity, trades)
        if target_regimes:
            conditioned = regime_conditioned_objective(
                equity,
                trades,
                labels=regime_labels,
                target_regimes=target_regimes,
                weights=weights,
            )
            audit_regime[side] = {
                key: value
                for key, value in conditioned.items()
                if not key.endswith("_daily")
            }
    audit_delta = utility(audit_rows["candidate"], weights) - utility(
        audit_rows["baseline"], weights
    )

    report = {
        "status": "ok",
        "folds": fold_rows,
        "fold_count": folds,
        "positive_folds": sum(1 for row in fold_rows if row["delta_utility"] > 0),
        "objective": {"baseline": baseline_vector, "candidate": candidate_vector},
        "paired_incumbent_delta": {
            "estimate": delta_estimate,
            "lcb": delta_lcb,
            "confidence": float(evaluation["confidence"]),
            "paired_days": len(deltas),
            "p_value": growth_p_value,
        },
        "audit_slice": {
            "start": str(timestamps[dev_total]),
            "end": str(timestamps[total - 1]),
            "bars": audit_bars,
            "baseline": audit_rows["baseline"],
            "candidate": audit_rows["candidate"],
            "delta_utility": audit_delta,
        },
    }
    if target_regimes:
        regime_config = evaluation.get("regime") or {}
        pooled: dict[str, dict[str, Any]] = {}
        for side in ("baseline", "candidate"):
            target_daily = list(regime_pools[side]["target_daily"])
            outside_daily = list(regime_pools[side]["outside_daily"])
            target_vector = _conditional_vector(
                [float(value) for _, value in target_daily],
                int(regime_pools[side]["target_entries"]),
            )
            outside_vector = _conditional_vector(
                [float(value) for _, value in outside_daily],
                int(regime_pools[side]["outside_entries"]),
            )
            pooled[side] = {
                "target": target_vector,
                "outside": {
                    **outside_vector,
                    "loss_pct": max(
                        0.0,
                        1.0 - math.exp(float(outside_vector["net_log_growth"])),
                    ),
                },
                "target_utility": utility(target_vector, weights),
                "outside_utility": utility(outside_vector, weights),
                "target_daily": target_daily,
            }
        target_deltas = paired_daily_deltas(
            list(pooled["baseline"]["target_daily"]),
            list(pooled["candidate"]["target_daily"]),
        )
        regime_block = int(regime_config.get("bootstrap_block_days") or 2)
        target_lcb = block_bootstrap_lcb(
            target_deltas,
            block_len=regime_block,
            iterations=int(evaluation["bootstrap_iterations"]),
            confidence=float(evaluation["confidence"]),
        )
        target_estimate = float(pooled["candidate"]["target_utility"]) - float(
            pooled["baseline"]["target_utility"]
        )
        audit_target_delta = float(audit_regime["candidate"]["target_utility"]) - float(
            audit_regime["baseline"]["target_utility"]
        )
        report["regime_contract"] = {
            "enabled": True,
            "classifier": PORTFOLIO_REGIME_CLASSIFIER,
            "target_regimes": list(target_regimes),
            "objective": pooled,
            "paired_target_delta": {
                "estimate": target_estimate,
                "lcb": target_lcb,
                "confidence": float(evaluation["confidence"]),
                "paired_days": len(target_deltas),
            },
            "audit": {
                "baseline": audit_regime["baseline"],
                "candidate": audit_regime["candidate"],
                "delta_utility": audit_target_delta,
            },
        }
    return report


def evaluate_economic_readiness(
    report: Mapping[str, Any],
    constitution: Mapping[str, Any],
    *,
    probation: bool = False,
) -> dict[str, Any]:
    """Map paired evidence to ready/reasons under the constitution. Pure
    policy — no simulation — so enforcement stays trivially testable."""
    promotion = constitution["promotion"]
    hard = constitution["hard_constraints"]
    reasons: list[str] = []
    if report.get("status") != "ok":
        reasons.append(f"economic evaluation unavailable: {report.get('detail')}")
        return {"ready": False, "reasons": reasons, "probation": probation}

    candidate = report["objective"]["candidate"]
    regime = report.get("regime_contract") or {}
    specialized = bool(regime.get("enabled"))
    target_candidate = ((regime.get("objective") or {}).get("candidate") or {}).get(
        "target"
    ) or {}
    decision_candidate = target_candidate if specialized else candidate
    if float(candidate["max_drawdown_pct"]) > float(hard["max_drawdown_pct"]):
        reasons.append(
            f"OOS max drawdown {candidate['max_drawdown_pct']:.3f} exceeds "
            f"ceiling {hard['max_drawdown_pct']}"
        )
    if float(candidate["tail_loss"]) > float(hard["max_tail_loss"]):
        reasons.append(
            f"OOS tail loss {candidate['tail_loss']:.3f} exceeds ceiling "
            f"{hard['max_tail_loss']}"
        )
    if int(decision_candidate.get("trade_count") or 0) < int(
        promotion["min_oos_trades"]
    ):
        reasons.append(
            f"OOS {'target-regime ' if specialized else ''}trade count "
            f"{decision_candidate.get('trade_count') or 0} below minimum "
            f"{promotion['min_oos_trades']}"
        )
    if specialized:
        regime_config = constitution["evaluation"].get("regime") or {}
        min_days = int(regime_config.get("min_target_days") or 5)
        target_days = int(decision_candidate.get("day_count") or 0)
        if target_days < min_days:
            reasons.append(
                f"target-regime paired days {target_days} below minimum {min_days}"
            )
        target_utility = float(
            (
                ((regime.get("objective") or {}).get("candidate") or {}).get(
                    "target_utility"
                )
            )
            or 0.0
        )
        if target_utility <= 0:
            reasons.append(
                f"absolute target-regime utility {target_utility:.4f} not > 0"
            )
        outside_loss = float(
            (
                (
                    ((regime.get("objective") or {}).get("candidate") or {}).get(
                        "outside"
                    )
                    or {}
                ).get("loss_pct")
            )
            or 0.0
        )
        outside_budget = float(regime_config.get("max_out_of_regime_loss_pct") or 0.02)
        if outside_loss > outside_budget:
            reasons.append(
                f"out-of-regime loss {outside_loss:.4f} exceeds budget "
                f"{outside_budget:.4f}"
            )
    if int(report["positive_folds"]) < int(promotion["required_positive_folds"]):
        reasons.append(
            f"positive OOS folds {report['positive_folds']}/{report['fold_count']} "
            f"below required {promotion['required_positive_folds']}"
        )
    delta = (
        regime["paired_target_delta"]
        if specialized
        else report["paired_incumbent_delta"]
    )
    require_lcb = not probation or bool(promotion["probation_requires_lcb"])
    if require_lcb:
        if delta["lcb"] is None:
            reasons.append("paired delta LCB unavailable (too few paired days)")
        elif float(delta["lcb"]) <= 0:
            reasons.append(
                f"paired utility delta LCB {delta['lcb']:.4f} not > 0 at "
                f"{delta['confidence']:.0%} confidence"
            )
    elif float(delta["estimate"]) <= 0:
        reasons.append(
            f"paired utility delta estimate {delta['estimate']:.4f} not > 0 "
            "(probation bar)"
        )
    audit_delta = float(
        ((regime.get("audit") or {}).get("delta_utility") or 0.0)
        if specialized
        else report["audit_slice"]["delta_utility"]
    )
    if specialized:
        audit_candidate = (regime.get("audit") or {}).get("candidate") or {}
        audit_target_utility = float(audit_candidate.get("target_utility") or 0.0)
        if audit_target_utility <= 0:
            reasons.append(
                f"audit target-regime utility {audit_target_utility:.4f} not > 0"
            )
        audit_outside_loss = float(
            ((audit_candidate.get("outside") or {}).get("loss_pct")) or 0.0
        )
        outside_budget = float(
            (constitution["evaluation"].get("regime") or {}).get(
                "max_out_of_regime_loss_pct"
            )
            or 0.02
        )
        if audit_outside_loss > outside_budget:
            reasons.append(
                f"audit out-of-regime loss {audit_outside_loss:.4f} exceeds "
                f"budget {outside_budget:.4f}"
            )
    if audit_delta < float(promotion["audit_min_delta_utility"]):
        reasons.append(
            f"audit-slice utility delta {audit_delta:.4f} below floor "
            f"{promotion['audit_min_delta_utility']}"
        )
    return {"ready": not reasons, "reasons": reasons, "probation": probation}


def _oos_window(
    script: str | Path | Callable[..., Any],
    dataset: PreparedExecutionDataset,
    spec: ExecutionSpec,
    params: Mapping[str, Any],
    timestamps: list[pd.Timestamp],
    test_start: int,
    test_end: int,
    warmup_bars: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    eval_start = max(0, test_start - warmup_bars)
    window = _slice(dataset, timestamps, eval_start, test_end)
    result = simulate_execution(script, window, spec, dict(params))
    boundary = timestamps[test_start]
    equity = [
        row for row in result.equity_curve if pd.Timestamp(row["timestamp"]) >= boundary
    ]
    trades = [
        row for row in result.trades if pd.Timestamp(row["timestamp"]) >= boundary
    ]
    return equity, trades


def _bar_seconds(spec: ExecutionSpec) -> int:
    from wayfinder_paths.jobs.execution.primitives import bar_interval_seconds

    seconds = bar_interval_seconds(spec.data_contract.get("bar_interval"))
    if seconds is None:
        raise ValueError("execution spec requires a positive bar_interval")
    return seconds


def _max_drawdown(equity_curve: Sequence[Mapping[str, Any]]) -> float:
    peak = 0.0
    worst = 0.0
    for row in equity_curve:
        value = float(row["equity"])
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst
