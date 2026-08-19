"""Selectable mixed-asset starter catalog and jobs_v1 implementations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.compiler import JobCompiler
from wayfinder_paths.jobs.execution.features import apply_precompute
from wayfinder_paths.jobs.execution.primitives import (
    CompletedBarsView,
    ExecutionContext,
    ExecutionSpec,
    PositionLedger,
    PositionRecord,
    StateSnapshot,
)
from wayfinder_paths.jobs.starters import create_starter_job, starter_catalog
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.strategies.mixed_bollinger_pullback import (
    MixedBollingerPullbackStrategy,
)
from wayfinder_paths.jobs.strategies.mixed_low_vol_rank import (
    MixedLowVolRankStrategy,
)
from wayfinder_paths.jobs.strategies.mixed_momentum_rank import (
    MixedMomentumRankStrategy,
)
from wayfinder_paths.jobs.strategies.mixed_rsi_snapback import (
    MixedRsiSnapbackStrategy,
)
from wayfinder_paths.jobs.strategies.mixed_sleeve_momentum import (
    MixedSleeveMomentumStrategy,
)
from wayfinder_paths.jobs.strategies.mixed_volume_capitulation import (
    MixedVolumeCapitulationStrategy,
)
from wayfinder_paths.jobs.strategies.pair_relative_strength import (
    PairRelativeStrengthStrategy,
)


def _context(
    strategy: Any,
    closes: dict[str, list[float]],
    *,
    interval: str,
    volumes: dict[str, list[float]] | None = None,
) -> ExecutionContext:
    timestamps = pd.date_range(
        "2026-01-01T00:00:00Z",
        periods=len(next(iter(closes.values()))),
        freq=interval,
    )
    rows: list[dict[str, Any]] = []
    for symbol, values in closes.items():
        symbol_volumes = (volumes or {}).get(symbol, [1.0] * len(values))
        for timestamp, close, volume in zip(
            timestamps, values, symbol_volumes, strict=True
        ):
            rows.append(
                {
                    "timestamp": timestamp,
                    "symbol": symbol,
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "volume": volume,
                }
            )
    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = interval
    view = apply_precompute(strategy, CompletedBarsView.from_rows(rows))
    return ExecutionContext(
        view=view,
        ledger=PositionLedger(),
        state_snapshot=StateSnapshot(status="valid"),
        capacity=None,
        params={"initial_capital": 10_000.0},
        timestamp=timestamps[-1].isoformat(),
        execution_spec=spec,
    )


def _sides(intents: list[dict[str, Any]]) -> dict[str, str]:
    return {intent["symbol"]: intent["side"] for intent in intents}


def test_starter_catalog_has_six_mixed_and_two_pair_paper_strategies() -> None:
    catalog = starter_catalog()
    assert len(catalog) == 8
    assert {item["timeframe"] for item in catalog} == {"15m", "1h", "1d"}
    assert [item["timeframe"] for item in catalog].count("15m") == 2
    assert [item["timeframe"] for item in catalog].count("1h") == 4
    assert [item["timeframe"] for item in catalog].count("1d") == 2
    for item in catalog:
        assert item["crypto_assets"]
        assert set(item["symbols"]) == set(item["crypto_assets"]) | set(
            item["tokenized_equities"]
        )
        assert item["default_mode"] == "paper"
        assert item["execution_contract"] == "jobs_v1"
        assert item["family"] in {
            "cross_sectional_momentum",
            "mean_reversion",
            "low_volatility_ranking",
            "relative_value_pair",
        }
        assert isinstance(item["cautions"], list)
        assert item["strategy_inception_at"]
        assert item["risk_limits"]["pause_after_consecutive_losses"] == 5
        assert 0 < item["params"]["stop_min_pct"] <= item["params"]["stop_max_pct"]
        assert item["params"]["native_stop_required"] is True
        assert item["risk_controls"]["account_halt"]["flatten_on_breach"] is False
        assert (
            item["research_evidence"]["risk_overlay_backtest_status"]
            == "pending_revalidation"
        )
        assert item["research_evidence"]["return_after_costs_and_funding"] > 0
        engine = item["research_evidence"]["jobs_v1_engine"]
        assert engine["return_after_fees_and_slippage"] > 0
        assert engine["funding_included"] is False
        assert engine["trace_valid"] is True

    pairs = {
        item["id"]: item
        for item in catalog
        if item["research_evidence"].get("strategy_family")
        == "cross-sectional pair momentum"
    }
    assert set(pairs) == {
        "btc-eth-relative-strength-1d",
        "bch-ltc-relative-strength-1d",
    }
    assert all(len(item["symbols"]) == 2 for item in pairs.values())
    assert all(
        item["risk_controls"]["pair_group_stop"]["cross_symbol_atomic"] is False
        for item in pairs.values()
    )
    assert all(not item["tokenized_equities"] for item in pairs.values())
    assert all(
        item["research_evidence"]["price_mean_reversion_gate"]["verdict"] == "REJECT"
        for item in pairs.values()
    )


def test_momentum_rank_longs_leaders_and_shorts_laggards() -> None:
    symbols = ["A", "B", "C", "D"]
    strategy = MixedMomentumRankStrategy(
        {
            "symbols": symbols,
            "momentum_bars": 2,
            "rebalance_bars": 1,
            "min_trade_notional": 0.0,
        }
    )
    ctx = _context(
        strategy,
        {
            "A": [1.0, 1.1, 1.2, 1.3, 1.5, 1.8, 2.2],
            "B": [1.0, 1.05, 1.1, 1.15, 1.2, 1.3, 1.4],
            "C": [1.0, 0.98, 0.96, 0.94, 0.92, 0.88, 0.84],
            "D": [1.0, 0.95, 0.9, 0.85, 0.8, 0.7, 0.6],
        },
        interval="1h",
    )
    assert _sides(strategy.decide(ctx)) == {
        "A": "buy",
        "B": "buy",
        "C": "sell",
        "D": "sell",
    }


def test_sleeve_momentum_keeps_crypto_and_equity_sleeves_separate() -> None:
    strategy = MixedSleeveMomentumStrategy(
        {
            "symbols": ["A", "B", "C", "D"],
            "sleeves": [["A", "B"], ["C", "D"]],
            "momentum_bars": 2,
            "rebalance_bars": 1,
            "min_trade_notional": 0.0,
        }
    )
    ctx = _context(
        strategy,
        {
            "A": [1.0, 1.05, 1.1, 1.2, 1.35, 1.55, 1.8],
            "B": [1.0, 0.98, 0.95, 0.9, 0.85, 0.78, 0.7],
            "C": [1.0, 0.97, 0.94, 0.9, 0.84, 0.77, 0.7],
            "D": [1.0, 1.04, 1.08, 1.15, 1.27, 1.42, 1.6],
        },
        interval="15min",
    )
    assert _sides(strategy.decide(ctx)) == {
        "A": "buy",
        "B": "sell",
        "C": "sell",
        "D": "buy",
    }


def test_low_vol_rank_longs_calm_pair_and_shorts_volatile_pair() -> None:
    strategy = MixedLowVolRankStrategy(
        {
            "symbols": ["A", "B", "C", "D"],
            "volatility_bars": 3,
            "rebalance_bars": 1,
            "min_trade_notional": 0.0,
        }
    )
    ctx = _context(
        strategy,
        {
            "A": [1.0, 1.01, 1.02, 1.03, 1.04, 1.05, 1.06, 1.07],
            "B": [1.0, 1.02, 1.01, 1.03, 1.02, 1.04, 1.03, 1.05],
            "C": [1.0, 1.20, 0.90, 1.30, 0.80, 1.25, 0.85, 1.20],
            "D": [1.0, 0.75, 1.25, 0.70, 1.35, 0.65, 1.40, 0.60],
        },
        interval="15min",
    )
    assert _sides(strategy.decide(ctx)) == {
        "A": "buy",
        "B": "buy",
        "C": "sell",
        "D": "sell",
    }


def test_rsi_snapback_requires_oversold_reading_and_positive_trend() -> None:
    strategy = MixedRsiSnapbackStrategy(
        {
            "symbols": ["A"],
            "rsi_period": 2,
            "entry_rsi": 101.0,
            "trend_sma_period": 3,
            "min_trade_notional": 0.0,
        }
    )
    ctx = _context(strategy, {"A": [1.0, 1.0, 1.0, 2.0, 2.1, 2.0, 2.2]}, interval="1h")
    assert _sides(strategy.decide(ctx)) == {"A": "buy"}


def test_bollinger_pullback_fades_extremes_inside_the_slow_trend() -> None:
    strategy = MixedBollingerPullbackStrategy(
        {
            "symbols": ["A", "B"],
            "zscore_bars": 3,
            "entry_zscore": 0.5,
            "trend_sma_period": 5,
            "min_trade_notional": 0.0,
        }
    )
    ctx = _context(
        strategy,
        {
            "A": [5, 6, 7, 8, 9, 10, 11, 12, 13, 12],
            "B": [15, 14, 13, 12, 11, 10, 9, 8, 7, 8],
        },
        interval="1h",
    )
    assert _sides(strategy.decide(ctx)) == {"A": "buy", "B": "sell"}


def test_volume_capitulation_requires_above_median_volume() -> None:
    strategy = MixedVolumeCapitulationStrategy(
        {
            "symbols": ["A", "B"],
            "rsi_period": 2,
            "entry_rsi": 101.0,
            "trend_sma_period": 3,
            "volume_median_bars": 3,
            "min_trade_notional": 0.0,
        }
    )
    ctx = _context(
        strategy,
        {
            "A": [1.0, 1.0, 1.0, 2.0, 2.1, 2.0, 2.2],
            "B": [1.0, 1.0, 1.0, 2.0, 2.1, 2.0, 2.2],
        },
        volumes={
            "A": [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 2.0],
            "B": [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
        },
        interval="1h",
    )
    assert _sides(strategy.decide(ctx)) == {"A": "buy"}


def test_pair_relative_strength_emits_two_opposite_equal_notional_legs() -> None:
    strategy = PairRelativeStrengthStrategy(
        {
            "symbols": ["A", "B"],
            "momentum_bars": 2,
            "volatility_bars": 3,
            "min_gross_exposure": 0.4,
            "max_gross_exposure": 0.4,
            "rebalance_bars": 1,
            "min_trade_notional": 0.0,
        }
    )
    ctx = _context(
        strategy,
        {
            "A": [1.0, 1.01, 1.03, 1.06, 1.10, 1.15, 1.21, 1.28],
            "B": [1.0, 1.00, 0.99, 1.00, 1.01, 1.00, 1.01, 1.00],
        },
        interval="1D",
    )
    intents = strategy.decide(ctx)
    assert _sides(intents) == {"A": "buy", "B": "sell"}
    assert {round(float(intent["notional"]), 2) for intent in intents} == {2000.0}


def test_pair_relative_strength_closes_an_orphan_leg_immediately() -> None:
    strategy = PairRelativeStrengthStrategy({"symbols": ["A", "B"]})
    ctx = _context(
        strategy,
        {"A": [1.0, 1.1], "B": [1.0, 0.9]},
        interval="1D",
    )
    ctx.ledger.positions["A"] = PositionRecord(
        symbol="A", side="long", size=2.0, avg_price=1.0
    )
    assert strategy.decide(ctx) == [
        {
            "action": "CLOSE",
            "venue": "hyperliquid",
            "symbol": "A",
            "side": "sell",
            "size": 2.0,
            "reduce_only": True,
            "metadata": {"exit_reason": "orphan_leg_guard"},
        }
    ]


def test_create_starter_materializes_job_and_forward_inception(tmp_path) -> None:
    store = JobStore(repo_root=tmp_path)
    result = create_starter_job(
        "mixed-momentum-rank-1h",
        job_id="my-starter",
        store=store,
        compile_job=False,
        initializer_session_id="ses_strategy-lab!bad",
    )
    job = store.load("my-starter")
    assert job.execution_contract == "jobs_v1"
    assert job.script_loop.mode == "paper"
    assert job.controller["starter"]["paper_only"] is True
    assert job.controller["starter"]["risk_limits"]["max_drawdown"] == -0.20
    assert job.controller["initializer_session_id"] == "ses_strategy-labbad"
    assert result["created"] is True
    assert job.execution_spec["data_contract"]["bar_interval"] == "1h"
    assert result["script_entrypoint"].endswith("workspace/src/strategy.py")
    assert "mixed_momentum_rank" in Path(result["script_entrypoint"]).read_text(
        encoding="utf-8"
    )
    summary = json.loads(
        (store.job_dir(job.id) / "results/forward/summary.json").read_text()
    )
    assert summary["inception_at"] == job.created_at
    assert (store.job_dir(job.id) / "results/backtest/starter_evidence.json").exists()
    assert json.loads(
        (store.job_dir(job.id) / "workspace/risk_limits.json").read_text()
    ) == {"max_drawdown": -0.2, "pause_after_consecutive_losses": 5}

    pair_result = create_starter_job(
        "btc-eth-relative-strength-1d",
        job_id="daily-pair-starter",
        store=store,
        compile_job=False,
    )
    pair_job = store.load("daily-pair-starter")
    assert pair_job.script_loop.interval_seconds == 86_400
    assert pair_job.execution_params["protection_monitor_interval_seconds"] == 300
    assert pair_job.execution_spec["data_contract"]["bar_interval"] == "1d"
    assert pair_job.execution_spec["data_contract"]["symbols"] == ["BTC", "ETH"]
    assert "pair_relative_strength" in Path(pair_result["script_entrypoint"]).read_text(
        encoding="utf-8"
    )


def test_create_starter_reuses_its_canonical_job_id(tmp_path) -> None:
    store = JobStore(repo_root=tmp_path)
    first = create_starter_job(
        "mixed-rsi-snapback-1h",
        store=store,
        compile_job=False,
        initializer_session_id="ses_first",
    )
    second = create_starter_job(
        "mixed-rsi-snapback-1h",
        store=store,
        compile_job=False,
        initializer_session_id="ses_second",
    )

    assert first["created"] is True
    assert second["created"] is False
    assert second["job"]["id"] == "mixed-rsi-snapback-1h"
    assert second["job"]["controller"]["initializer_session_id"] == "ses_first"


def test_live_pair_compiles_at_protection_monitor_cadence(
    tmp_path, monkeypatch
) -> None:
    calls: list[dict[str, Any]] = []

    class FakeBridge:
        def __init__(self, *, repo_root=None):  # noqa: ANN001
            self.repo_root = repo_root

        def add_or_update_script_job(self, **kwargs):  # noqa: ANN003
            calls.append(kwargs)
            return {"ok": True}

    monkeypatch.setattr("wayfinder_paths.jobs.compiler.RunnerBridge", FakeBridge)
    store = JobStore(repo_root=tmp_path)
    create_starter_job(
        "btc-eth-relative-strength-1d",
        job_id="protected-pair",
        store=store,
        compile_job=False,
    )
    job = store.load("protected-pair")
    job.script_loop.mode = "live"
    store.save(job)

    JobCompiler(store=store).compile(job, start_daemon=False)

    script_call = next(call for call in calls if call["name"].endswith("-script"))
    assert script_call["interval_seconds"] == 300
