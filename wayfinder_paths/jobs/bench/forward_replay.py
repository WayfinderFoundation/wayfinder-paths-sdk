"""Production-engine bundle races and compressed probation replay."""

from __future__ import annotations

import asyncio
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.bench.env import atomic_json, git_sha
from wayfinder_paths.jobs.bench.world import execution_identity_sha256, load_world
from wayfinder_paths.jobs.bundles import resolve_bundle_script_entrypoint
from wayfinder_paths.jobs.candidate_shadow import run_candidate_shadows
from wayfinder_paths.jobs.economics import block_bootstrap_lcb
from wayfinder_paths.jobs.execution.features import load_feature_rows, merge_features
from wayfinder_paths.jobs.execution.job import _load_job_yaml, _store_feature_specs
from wayfinder_paths.jobs.execution.primitives import (
    CompletedBarsView,
    ExecutionSpec,
)
from wayfinder_paths.jobs.execution.simulator import (
    PreparedExecutionDataset,
    simulate_execution,
)
from wayfinder_paths.jobs.execution.validation import (
    resolve_execution_spec,
    window_invariance_probe,
)
from wayfinder_paths.jobs.execution.walk_forward import _test_window_stats
from wayfinder_paths.jobs.gating import compute_workspace_revision
from wayfinder_paths.jobs.probation import load_probation, maybe_adjudicate_probation
from wayfinder_paths.jobs.regime import (
    classify_portfolio_regimes,
    declared_regimes,
    partition_regime_returns,
    regime_universe,
    utc_timestamp,
)
from wayfinder_paths.jobs.store import JobStore

PARTICIPATION_FLOOR = 10
CONFIDENCE = 0.90


def race_bundles(
    a_bundle: Path,
    b_bundle: Path,
    *,
    world_dir: Path,
    sealed_dir: Path,
    output_dir: Path | None = None,
    feature_root: Path | None = None,
) -> dict[str, Any]:
    """Evaluate A and B on identical frozen rows and pre-written rules.
    ``feature_root`` is the job whose feature store (derived columns such as
    the macro regime) both bundles may declare and read."""
    world = load_world(world_dir, sealed_dir)
    cutoff = _parse(world["manifest"]["generation_cutoff"])
    rows = [
        *(world["development"].get("bars") or []),
        *(world["holdout"].get("bars") or []),
    ]
    environment = dict(world["manifest"].get("execution_environment") or {})
    a = evaluate_bundle(
        a_bundle,
        rows=rows,
        cutoff=cutoff,
        environment=environment,
        feature_root=feature_root,
    )
    b = evaluate_bundle(
        b_bundle,
        rows=rows,
        cutoff=cutoff,
        environment=environment,
        feature_root=feature_root,
    )
    days = sorted(set(a["daily_pnl"]) & set(b["daily_pnl"]))
    deltas = paired_daily_utility_deltas(
        a["daily_pnl"], b["daily_pnl"], capital=environment_capital(environment)
    )
    estimate = sum(deltas)
    lcb = block_bootstrap_lcb(
        deltas, block_len=5, iterations=500, confidence=CONFIDENCE
    )
    a_participates = int(a["stats"].get("trade_count") or 0) >= PARTICIPATION_FLOOR
    b_participates = int(b["stats"].get("trade_count") or 0) >= PARTICIPATION_FLOOR
    a_drawdown = abs(float(a["stats"].get("max_drawdown_pct") or 0.0))
    b_drawdown = abs(float(b["stats"].get("max_drawdown_pct") or 0.0))
    drawdown_ok = a_drawdown <= max(0.000001, 1.25 * b_drawdown)
    valid = bool(
        a["valid"] and b["valid"] and days and a_participates and b_participates
    )
    if not valid:
        verdict = "invalid"
    elif lcb is not None and lcb > 0 and drawdown_ok:
        verdict = "a_beats_b"
    else:
        verdict = "no_significant_difference"
    compare = {
        "schema_version": "1.0",
        "world_id": world["manifest"]["world_id"],
        "sdk_ref": git_sha(Path(__file__).resolve().parents[3]),
        "dataset_sha256": world["manifest"]["dataset"].get(
            "benchmark_rows_sha256",
            world["manifest"]["dataset"]["full_rows_sha256"],
        ),
        "generation_cutoff": cutoff.isoformat(),
        "rules": {
            "confidence": CONFIDENCE,
            "participation_floor": PARTICIPATION_FLOOR,
            "max_drawdown_ratio": 1.25,
            "verdict": (
                "A beats B iff paired LCB > 0, both sides meet the participation "
                "floor, and A drawdown is no more than 1.25x B"
            ),
        },
        "paired_daily_utility_delta": {
            "days": len(deltas),
            "estimate": round(estimate, 8),
            "lcb": lcb,
            "values": [round(value, 10) for value in deltas],
        },
        "participation": {
            "a": a_participates,
            "b": b_participates,
        },
        "drawdown_check": {
            "a": a_drawdown,
            "b": b_drawdown,
            "passed": drawdown_ok,
        },
        "behavior_distance": _behavior_distance(a["trades"], b["trades"]),
        "a": _public_side(a),
        "b": _public_side(b),
        "verdict": verdict,
    }
    if output_dir is not None:
        results = output_dir / "results"
        results.mkdir(parents=True, exist_ok=True)
        _write_side(results / "a", a)
        _write_side(results / "b", b)
        atomic_json(results / "compare.json", compare)
        (results / "report.txt").write_text(_race_report(compare), encoding="utf-8")
    return compare


def paired_daily_utility_deltas(
    a_daily: Mapping[str, float],
    b_daily: Mapping[str, float],
    *,
    capital: float = 10_000.0,
) -> list[float]:
    """Per paired ISO day, log-growth utility of A minus B on the book's
    capital — the same quantity probation adjudicates on."""
    days = sorted(set(a_daily) & set(b_daily))
    return [
        math.log1p(float(a_daily[day]) / capital)
        - math.log1p(float(b_daily[day]) / capital)
        for day in days
        if float(a_daily[day]) > -capital and float(b_daily[day]) > -capital
    ]


def environment_capital(environment: Mapping[str, Any] | None) -> float:
    params = (environment or {}).get("params") or {}
    return float(params.get("initial_capital") or 10_000.0)


def evaluate_bundle(
    bundle: Path,
    *,
    rows: Sequence[Mapping[str, Any]],
    cutoff: datetime,
    environment: dict[str, Any] | None = None,
    feature_root: Path | None = None,
) -> dict[str, Any]:
    bundle = bundle.resolve()
    before = compute_workspace_revision(bundle)
    job_data = _load_job_yaml(bundle)
    spec_data, _ = resolve_execution_spec(bundle, job_data)
    if not spec_data:
        raise FileNotFoundError(f"bundle has no execution_spec: {bundle}")
    spec = ExecutionSpec.from_dict(spec_data)
    params = dict(job_data.get("execution_params") or {})
    environment = environment or {}
    expected_spec = environment.get("spec_sha256")
    spec_matches = not expected_spec or execution_identity_sha256(spec) == expected_spec
    params.update(dict(environment.get("params") or {}))
    script = resolve_bundle_script_entrypoint(bundle, job_data)
    dataset = PreparedExecutionDataset.from_rows(
        list(rows), {"source": "sealed_benchmark_world"}
    )
    if feature_root is not None:
        dataset = PreparedExecutionDataset(
            merge_store_features(dataset.bars, feature_root),
            dict(dataset.metadata),
            list(dataset.market_events),
        )
    probe = window_invariance_probe(script, dataset.bars, spec, params)
    result = simulate_execution(script, dataset, spec, params)
    stats = _test_window_stats(result, pd.Timestamp(cutoff), spec, params)
    trades = [
        trade
        for trade in result.trades
        if _trade_timestamp(trade) > pd.Timestamp(cutoff)
    ]
    target_regimes = declared_regimes(params)
    if target_regimes:
        classified = classify_portfolio_regimes(
            dataset.bars.to_frame(),
            universe=regime_universe(params, dataset.bars.symbols),
        )
        labels = {
            utc_timestamp(stamp): str(label) for stamp, label in classified.items()
        }
        stats["regime"] = {
            "target_regimes": list(target_regimes),
            **partition_regime_returns(
                _equity_window(result.equity_curve, cutoff=cutoff),
                trades,
                labels=labels,
                target_regimes=target_regimes,
            ),
        }
    after = compute_workspace_revision(bundle)
    return {
        "revision_before": before,
        "revision_after": after,
        "valid": bool(
            before == after
            and spec_matches
            and probe.get("status") == "passed"
            and result.validation.get("execution_valid") is True
        ),
        "spec_matches_world": spec_matches,
        "window_invariance": {
            key: value for key, value in probe.items() if not key.endswith("_intents")
        },
        "validation": result.validation,
        "stats": stats,
        "daily_pnl": _daily_pnl(
            result.equity_curve,
            cutoff=cutoff,
            capital=environment_capital(environment),
        ),
        "trades": trades,
        "per_symbol_direction": _per_symbol_direction(trades),
        "profile": result.profile,
        "equity_curve": result.equity_curve,
    }


def merge_store_features(view: CompletedBarsView, root: Path) -> CompletedBarsView:
    """As-of merge of every feature in the job's store onto a replay view.
    Replays build their bars from frozen rows, not the driver, so a strategy
    that declares a derived column (the macro regime) would otherwise meet a
    view without it and fail on the first tick."""
    specs = _store_feature_specs((root,), set())
    if not specs:
        return view
    frames = load_feature_rows([root], specs)
    return merge_features(view, frames, specs)


def replay_probation(
    store: JobStore,
    job_id: str,
    *,
    development_rows: list[dict[str, Any]],
    holdout_rows: list[dict[str, Any]],
    generation_cutoff: datetime,
    campaign_id: str | None = None,
) -> dict[str, Any]:
    """Replay staged trials through the real burn-in/day-7/day-14 code.

    ``campaign_id`` names the trial rebased to ``generation_cutoff``. Open
    trials from earlier loops keep their cursors and continue over the new
    window alongside it; their ids are returned as ``carried``.
    """
    cutoff = _aware(generation_cutoff)
    doc = load_probation(store, job_id)
    open_trials = [
        trial
        for trial in doc.get("trials") or []
        if trial.get("status") in {"queued", "burn_in", "active"}
    ]
    trial = next(
        (
            row
            for row in open_trials
            if campaign_id is None or row.get("campaign_id") == campaign_id
        ),
        None,
    )
    carried = [
        str(row["trial_id"])
        for row in open_trials
        if trial is None or row["trial_id"] != trial["trial_id"]
    ]
    unavailable = {
        "available": False,
        "reason": "campaign staged no probation candidate",
        "carried": carried,
        "paired_daily_delta": [],
    }
    if trial is None and not carried:
        return unavailable
    if trial is not None:
        _rebase_trial(trial, cutoff=cutoff)
        store.write_json(job_id, "probation.json", doc)
    warmup_rows = [
        row for row in development_rows if _row_timestamp(row) <= pd.Timestamp(cutoff)
    ][-2_000:]
    replay_rows: list[Mapping[str, Any]] = [*warmup_rows, *holdout_rows]
    view = merge_store_features(
        CompletedBarsView.from_rows(replay_rows), store.job_dir(job_id)
    )
    end = max(view.timestamps)
    asyncio.run(run_candidate_shadows(store, job_id, view=view, now=end))
    maybe_adjudicate_probation(store, job_id, now=end.to_pydatetime())
    if trial is None:
        return unavailable
    updated = next(
        row
        for row in load_probation(store, job_id)["trials"]
        if row["trial_id"] == trial["trial_id"]
    )
    metrics = (updated.get("forward") or {}).get("metrics") or {}
    return {
        "available": True,
        "trial_id": updated["trial_id"],
        "candidate_id": updated.get("candidate_id"),
        "status": updated.get("status"),
        "phase": updated.get("phase"),
        "burn_in": updated.get("burn_in"),
        "forward": updated.get("forward"),
        "carried": carried,
        "paired_daily_delta": metrics.get("daily_deltas") or [],
    }


def _rebase_trial(trial: dict[str, Any], *, cutoff: datetime) -> None:
    trial["status"] = "burn_in"
    trial["phase"] = "burn_in"
    trial["burn_in"].update(
        {
            "status": "running",
            "started_at": cutoff.isoformat(),
            "expires_at": (
                cutoff
                + timedelta(
                    hours=float(trial["burn_in"].get("duration_hours") or 24) + 12
                )
            ).isoformat(),
        }
    )
    trial["forward"].update(
        {
            "started_at": None,
            "deadline_at": None,
            "last_decision_day": 0,
            "metrics": None,
        }
    )
    for role in ("candidate", "reference"):
        trial[role]["last_processed_bar"] = cutoff.isoformat()
        trial[role]["error_count"] = 0


def _daily_pnl(
    equity_curve: list[dict[str, Any]],
    *,
    cutoff: datetime,
    capital: float = 10_000.0,
) -> dict[str, float]:
    cutoff_stamp = pd.Timestamp(cutoff)
    prior = [
        row for row in equity_curve if pd.Timestamp(row["timestamp"]) <= cutoff_stamp
    ]
    anchor = float(prior[-1]["equity"]) if prior else capital
    last_by_day: dict[str, float] = {}
    for row in equity_curve:
        stamp = pd.Timestamp(row["timestamp"])
        if stamp <= cutoff_stamp:
            continue
        last_by_day[stamp.date().isoformat()] = float(row["equity"])
    daily: dict[str, float] = {}
    for day, value in sorted(last_by_day.items()):
        daily[day] = value - anchor
        anchor = value
    return daily


def _equity_window(
    equity_curve: list[dict[str, Any]], *, cutoff: datetime
) -> list[dict[str, Any]]:
    """Holdout equity plus its last development anchor for first-bar returns."""
    cutoff_stamp = pd.Timestamp(cutoff)
    before = [
        row for row in equity_curve if pd.Timestamp(row["timestamp"]) <= cutoff_stamp
    ]
    after = [
        row for row in equity_curve if pd.Timestamp(row["timestamp"]) > cutoff_stamp
    ]
    return [*(before[-1:] if before else []), *after]


def _trade_timestamp(trade: dict[str, Any]) -> pd.Timestamp:
    return pd.Timestamp(trade.get("timestamp") or trade.get("filled_at"))


def _per_symbol_direction(trades: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, dict[str, float]] = defaultdict(
        lambda: {"fills": 0, "fees": 0.0, "realized_pnl": 0.0}
    )
    for trade in trades:
        symbol = str(trade.get("symbol") or "unknown")
        side = str(trade.get("side") or trade.get("action") or "unknown").lower()
        key = f"{symbol}:{side}"
        grouped[key]["fills"] += 1
        grouped[key]["fees"] += float(trade.get("fee") or 0.0)
        grouped[key]["realized_pnl"] += float(trade.get("realized_pnl_delta") or 0.0)
    return dict(grouped)


def _behavior_distance(
    a: list[dict[str, Any]], b: list[dict[str, Any]]
) -> dict[str, Any]:
    def decision_signatures(
        rows: list[dict[str, Any]],
    ) -> set[tuple[str, str, str]]:
        return {
            (
                str(row.get("timestamp") or row.get("filled_at")),
                str(row.get("symbol")),
                str(row.get("side") or row.get("action")),
            )
            for row in rows
        }

    def economic_signatures(
        rows: list[dict[str, Any]],
    ) -> set[tuple[str, str, str, float]]:
        return {
            (
                str(row.get("timestamp") or row.get("filled_at")),
                str(row.get("symbol")),
                str(row.get("side") or row.get("action")),
                round(float(row.get("filled_size") or row.get("size") or 0.0), 12),
            )
            for row in rows
        }

    left_decisions = decision_signatures(a)
    right_decisions = decision_signatures(b)
    left_economic = economic_signatures(a)
    right_economic = economic_signatures(b)
    changed_decisions = len(left_decisions ^ right_decisions)
    changed_fills = len(left_economic ^ right_economic)
    union = left_economic | right_economic
    return {
        "behavior_changed": bool(changed_decisions or changed_fills),
        "changed_decisions": changed_decisions,
        "changed_fill_records": changed_fills,
        "jaccard_distance": (round(changed_fills / len(union), 6) if union else 0.0),
    }


def _public_side(side: dict[str, Any]) -> dict[str, Any]:
    return {
        key: side[key]
        for key in (
            "revision_before",
            "revision_after",
            "valid",
            "validation",
            "spec_matches_world",
            "window_invariance",
            "stats",
            "daily_pnl",
            "per_symbol_direction",
            "profile",
        )
    }


def _write_side(root: Path, side: dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    atomic_json(root / "summary.json", _public_side(side))
    atomic_json(root / "trades.json", side["trades"])
    atomic_json(root / "equity.json", side["equity_curve"])


def _race_report(compare: dict[str, Any]) -> str:
    paired = compare["paired_daily_utility_delta"]
    return (
        f"Race verdict: {compare['verdict']}\n"
        f"World: {compare['world_id']}\n"
        f"Paired days: {paired['days']}\n"
        f"Utility delta estimate: {paired['estimate']}\n"
        f"90% LCB: {paired['lcb']}\n"
    )


def _row_timestamp(row: dict[str, Any]) -> pd.Timestamp:
    stamp = pd.Timestamp(row.get("timestamp", row.get("t")))
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _parse(value: Any) -> datetime:
    return _aware(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
