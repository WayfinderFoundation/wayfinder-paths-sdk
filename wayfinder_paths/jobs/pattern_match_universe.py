"""Create the shadow-first scheduled Pattern Match universe job."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.models import WayfinderJob, normalize_agent_mode, utc_now_iso
from wayfinder_paths.jobs.store import JobStore, safe_job_id
from wayfinder_paths.jobs.universe import fetch_hyperliquid_perp_universe

DEFAULT_JOB_ID = "pattern-match-universe-15m"
DEFAULT_MINIMUM_VOLUME_USD = 5_000_000.0


def create_pattern_match_universe_job(
    *,
    job_id: str = DEFAULT_JOB_ID,
    minimum_volume_usd: float = DEFAULT_MINIMUM_VOLUME_USD,
    store: JobStore | None = None,
    compile_job: bool = True,
    agent_mode: str = "intervene",
    fetch_universe: Callable[[], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Snapshot the current native + HIP-3 universe into a paper job.

    Discovery is repeated when this initializer is explicitly rerun; the
    execution job itself keeps a revision-bound symbol list so listings cannot
    silently change the strategy that was reviewed and backtested.
    """

    if minimum_volume_usd <= 0:
        raise ValueError("minimum_volume_usd must be positive")
    store = store or JobStore()
    resolved_id = safe_job_id(job_id)
    root = store.job_dir(resolved_id)
    if (root / "job.yaml").exists():
        raise FileExistsError(f"job already exists: {resolved_id}")
    listed = (
        fetch_universe()
        if fetch_universe is not None
        else asyncio.run(fetch_hyperliquid_perp_universe())
    )
    eligible = [
        dict(item)
        for item in listed
        if not bool(item.get("delisted"))
        and float(item.get("volume_24h_usd") or 0.0) > minimum_volume_usd
    ]
    eligible.sort(key=lambda item: -float(item["volume_24h_usd"]))
    symbols = [str(item["symbol"]) for item in eligible]
    if not symbols:
        raise RuntimeError("Hyperliquid discovery returned no liquid perp markets")
    from wayfinder_paths.jobs.strategies.pattern_match_universe import (
        load_calibration_bundle,
    )

    calibrated = set(load_calibration_bundle()["markets"])
    model_symbols = [symbol for symbol in symbols if symbol in calibrated]
    if not model_symbols:
        raise RuntimeError(
            "liquid universe contains no calibrated Pattern Match markets"
        )
    for item in eligible:
        item["model_calibrated"] = str(item["symbol"]) in calibrated

    job = WayfinderJob.new(
        resolved_id,
        name="Pattern Match liquid universe · 15m",
        goal=(
            "Evaluate every liquid Hyperliquid native and HIP-3 perp, score the "
            "calibrated subset, maintain causal market/direction lane health, and "
            "surface evidence before any proposal to enable orders."
        ),
        script="workspace/src/strategy.py",
        interval_seconds=15 * 60,
        timeout_seconds=12 * 60,
        agent_mode=normalize_agent_mode(agent_mode),
        agent_wake_seconds=6 * 60 * 60,
        execution_contract="jobs_v1",
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
            "bar_interval": "15m",
            "symbols": model_symbols,
            "max_bar_age_intervals": 2,
            "stale_policy": "skip",
        },
        "validation": {
            "mode": "strict",
            "require_scenarios": False,
        },
        "venues": ["hyperliquid"],
    }
    job.execution_params = {
        "symbols": model_symbols,
        "universe_symbols": symbols,
        "venue": "hyperliquid",
        # Endpoint boundaries can omit the first/forming buckets. The engine
        # uses warmup_bars as fetch depth; the model still caps at 10,000.
        "warmup_bars": 10_012,
        "minimum_history_bars": 10_000,
        "include_funding_context": True,
        "market_data_concurrency": 4,
        "minimum_volume_24h_usd": float(minimum_volume_usd),
        "allow_orders": False,
        "max_positions": 4,
        "notional_usd": 100.0,
        "initial_capital": 10_000.0,
        "fee_bps": 4.5,
        "slippage_bps": 3.5,
        "leverage": 1.0,
    }
    job.controller["pattern_match_universe"] = {
        "schema_version": 1,
        "observed_at": utc_now_iso(),
        "minimum_volume_24h_usd": float(minimum_volume_usd),
        "native_markets": sum(item.get("venue") == "native" for item in eligible),
        "hip3_markets": sum(item.get("venue") == "hip3" for item in eligible),
        "model_markets": len(model_symbols),
        "abstention_markets": len(symbols) - len(model_symbols),
        "paper_only": True,
        "orders_enabled": False,
        "universe": eligible,
    }
    job_path = store.create_job(job)
    entrypoint = store.resolve_script_entrypoint(job.id, job.to_dict())
    if entrypoint is None:
        raise RuntimeError("Pattern Match universe job has no strategy entrypoint")
    Path(entrypoint).write_text(
        "from wayfinder_paths.jobs.strategies.pattern_match_universe import "
        "build_strategy\n",
        encoding="utf-8",
    )
    store.write_json(
        job.id,
        "workspace/risk_limits.json",
        {
            "max_drawdown": -0.10,
            "pause_after_consecutive_losses": 5,
        },
    )
    result: dict[str, Any] = {
        "created": True,
        "job": job.to_dict(),
        "job_yaml": str(job_path),
        "script_entrypoint": str(entrypoint),
        "universe": {
            "markets": len(eligible),
            "native": job.controller["pattern_match_universe"]["native_markets"],
            "hip3": job.controller["pattern_match_universe"]["hip3_markets"],
            "model_markets": len(model_symbols),
            "abstention_markets": len(symbols) - len(model_symbols),
        },
    }
    if compile_job:
        from wayfinder_paths.jobs.compiler import JobCompiler
        from wayfinder_paths.jobs.sync import sync_all_jobs

        result["compile"] = JobCompiler(store=store).compile(job)
        sync_all_jobs(store=store)
    return result


__all__ = ["create_pattern_match_universe_job"]
