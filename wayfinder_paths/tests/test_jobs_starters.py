"""Selectable mixed-asset starter catalog and jobs_v1 implementations."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from wayfinder_paths.jobs import sync as sync_mod
from wayfinder_paths.jobs.compiler import JobCompiler
from wayfinder_paths.jobs.execution.features import apply_precompute
from wayfinder_paths.jobs.execution.job import _resolve_dataset
from wayfinder_paths.jobs.execution.primitives import (
    CompletedBarsView,
    ExecutionContext,
    ExecutionSpec,
    PositionLedger,
    PositionRecord,
    StateSnapshot,
)
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.starters import (
    STARTER_AGENT_WAKE_SECONDS,
    STARTER_CATALOG_VERSION,
    STARTER_DEFINITIONS,
    coerce_starter_leverage,
    create_starter_job,
    starter_catalog,
    starter_lookback_bars,
    starter_warmup_bars,
    validate_starter_leverage,
)
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.strategies._starter_utils import buffered_rank_weights
from wayfinder_paths.jobs.strategies.crypto_momentum_persistence import (
    CryptoMomentumPersistenceStrategy,
)
from wayfinder_paths.jobs.strategies.hype_passive_rsi import (
    HypePassiveRsiStrategy,
)
from wayfinder_paths.jobs.strategies.mixed_bollinger_pullback import (
    MixedBollingerPullbackStrategy,
)
from wayfinder_paths.jobs.strategies.mixed_factor_balance import (
    MixedFactorBalanceStrategy,
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
from wayfinder_paths.jobs.sync import snapshot_job


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


def test_hype_passive_rsi_emits_deep_alo_bid_with_fill_relative_stop() -> None:
    strategy = HypePassiveRsiStrategy()
    ctx = _context(
        strategy,
        {"HYPE": [100.0 - index for index in range(24)]},
        interval="5min",
    )

    intents = strategy.decide(ctx)

    assert len(intents) == 1
    entry = intents[0]
    assert entry["action"] == "OPEN"
    assert entry["time_in_force"] == "ALO"
    assert entry["expires_after_bars"] == 1
    assert entry["limit_price"] < 77.0
    assert entry["notional"] == 10_000.0
    assert entry["bracket"]["stop_loss_pct"] > 0
    assert entry["bracket"]["native_required"] is True


def test_hype_passive_rsi_honors_engine_stop_cooldown() -> None:
    strategy = HypePassiveRsiStrategy()
    ctx = _context(
        strategy,
        {"HYPE": [100.0 - index for index in range(24)]},
        interval="5min",
    )
    ctx.strategy_state["protection_cooldowns"] = {"HYPE": "2026-01-02T00:00:00+00:00"}

    assert strategy.decide(ctx) == []


def test_hype_passive_rsi_supports_full_and_staged_maker_exits() -> None:
    closes = {"HYPE": [100.0 - index for index in range(24)]}
    full = HypePassiveRsiStrategy({"exit_mode": "full"})
    full_ctx = _context(full, closes, interval="5min")
    full_ctx.ledger.positions["HYPE"] = PositionRecord(
        symbol="HYPE", side="long", size=10.0, avg_price=75.0
    )
    full_ctx.strategy_state["maker_entry"] = {"signal_atr": 1.0}

    full_exits = full.decide(full_ctx)

    assert len(full_exits) == 1
    assert full_exits[0]["size"] == 10.0
    assert full_exits[0]["limit_price"] == 76.5
    assert full_exits[0]["time_in_force"] == "ALO"

    staged = HypePassiveRsiStrategy(
        {"exit_mode": "staged", "move_stop_to_break_even": True}
    )
    staged_ctx = _context(staged, closes, interval="5min")
    staged_ctx.ledger.positions["HYPE"] = PositionRecord(
        symbol="HYPE", side="long", size=10.0, avg_price=75.0
    )
    staged_ctx.strategy_state["maker_entry"] = {"signal_atr": 1.0}

    staged_exits = staged.decide(staged_ctx)

    assert [intent["size"] for intent in staged_exits] == [5.0, 5.0]
    assert [intent["limit_price"] for intent in staged_exits] == [76.0, 76.5]
    assert staged_exits[0]["metadata"]["move_stop_to_break_even"] is True
    assert staged_exits[1]["metadata"]["move_stop_to_break_even"] is False


def _journal_events(store: JobStore, job_id: str) -> list[dict[str, Any]]:
    path = store.job_dir(job_id) / "journal.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.fixture(autouse=True)
def dataset_fetch_spawns(monkeypatch) -> list[dict[str, Any]]:
    """Starter creation spawns a real detached fetch child; stub it so every
    test stays hermetic, recording the calls for assertions."""
    calls: list[dict[str, Any]] = []

    def fake_spawn(store, job_id, op, kwargs):  # noqa: ANN001
        calls.append({"job_id": job_id, "op": op, "kwargs": kwargs})
        return {
            "started": True,
            "op": op,
            "job_id": job_id,
            "state": "running",
            "pid": 4242,
        }

    monkeypatch.setattr("wayfinder_paths.jobs.starters.spawn_detached_op", fake_spawn)
    return calls


def test_starter_catalog_has_mixed_maker_and_pair_paper_strategies() -> None:
    catalog = starter_catalog()
    assert len(catalog) == 13
    assert catalog[0]["id"] == "mixed-factor-balance-4h"
    assert {item["timeframe"] for item in catalog} == {
        "5m",
        "15m",
        "1h",
        "4h",
        "1d",
    }
    assert [item["timeframe"] for item in catalog].count("5m") == 2
    assert [item["timeframe"] for item in catalog].count("15m") == 2
    assert [item["timeframe"] for item in catalog].count("1h") == 5
    assert [item["timeframe"] for item in catalog].count("4h") == 2
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
            "maker_mean_reversion",
            "mean_reversion",
            "low_volatility_ranking",
            "multi_factor",
            "relative_value_pair",
        }
        assert isinstance(item["cautions"], list)
        assert item["strategy_inception_at"]
        assert item["risk_limits"]["pause_after_consecutive_losses"] == 5
        assert 0 < item["params"]["stop_min_pct"] <= item["params"]["stop_max_pct"]
        assert item["params"]["native_stop_required"] is True
        assert item["risk_controls"]["account_halt"]["flatten_on_breach"] is False
        assert item["leverage_control"] == {
            "minimum": 1,
            "maximum": 5,
            "step": 1,
            "default": 1,
            "operator_owned": True,
        }
        assert item["research_evidence"]["risk_overlay_backtest_status"] == "validated"
        assert (
            item["research_evidence"]["risk_overlay_backtest_scope"]
            == "per_position_ohlc_stops"
        )
        funded_return = item["research_evidence"].get("return_after_costs_and_funding")
        if item["research_evidence"].get("funding_included") is False:
            assert funded_return is None
        else:
            assert funded_return is not None and funded_return > 0
        engine = item["research_evidence"]["jobs_v1_engine"]
        assert engine["return_after_fees_and_slippage"] > 0
        assert isinstance(engine["funding_included"], bool)
        assert engine["trace_valid"] is True
        assert engine["full_period_vs_no_stop"] in {"unchanged", "improved"}
        if item["family"] == "maker_mean_reversion":
            assert 0 <= engine["chronological_folds_non_regressing"] <= 4
        elif item["family"] == "multi_factor":
            assert engine["chronological_folds_non_regressing"] == 3
        else:
            assert engine["chronological_folds_non_regressing"] == 4
        assert engine["stop_count"] >= 0
        sweep = item["research_evidence"]["jobs_v1_leverage_sweep"]
        assert sweep["leverage_semantics"] == "target_exposure"
        assert sweep["account_halt_simulated"] is False
        assert sweep["funding_included"] is engine["funding_included"]
        assert [row["leverage"] for row in sweep["results"]] == [1, 2, 3, 4, 5]
        assert all(row["liquidation_count"] == 0 for row in sweep["results"])
        assert sweep["results"][0]["return_after_fees_and_slippage"] == pytest.approx(
            engine["return_after_fees_and_slippage"], abs=0.0001
        )

    by_id = {item["id"]: item for item in catalog}
    crypto_momentum = by_id["crypto-momentum-persistence-4h"]
    assert crypto_momentum["params"]["score_volatility_bars"] == 168
    assert crypto_momentum["params"]["broad_bull_momentum_threshold"] == 0.10
    assert crypto_momentum["params"]["broad_bull_weight_shift"] == 0.175
    assert crypto_momentum["robustness_plan"]["walk_forward"] == {
        "train_bars": 1440,
        "test_bars": 360,
        "folds": 4,
    }
    assert crypto_momentum["research_evidence"]["sharpe"] > 1.0
    assert all(
        sharpe > 1.0
        for sharpe in crypto_momentum["research_evidence"][
            "rebalance_phase_sharpes"
        ].values()
    )
    expected_stops = {
        "mixed-factor-balance-4h": (12.0, 0.25, 0.50),
        "mixed-rsi-snapback-1h": (5.0, 0.08, 0.15),
        "mixed-momentum-rank-1h": (8.0, 0.15, 0.30),
        "crypto-momentum-persistence-4h": (12.0, 0.25, 0.50),
        "mixed-sleeve-momentum-15m": (14.0, 0.26, 0.52),
        "mixed-low-vol-rank-15m": (12.0, 0.25, 0.50),
        "balanced-passive-capitulation-1h": (5.0, 0.08, 0.15),
        "hype-passive-rsi-full-5m": (3.0, 0.001, 0.20),
        "hype-passive-rsi-staged-5m": (3.0, 0.001, 0.20),
        "btc-eth-relative-strength-1d": (15.0, 0.40, 0.60),
    }
    for starter_id, expected in expected_stops.items():
        params = by_id[starter_id]["params"]
        assert (
            params["stop_atr_multiple"],
            params["stop_min_pct"],
            params["stop_max_pct"],
        ) == expected

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

    makers = [item for item in catalog if item["family"] == "maker_mean_reversion"]
    assert {item["id"] for item in makers} == {
        "balanced-passive-capitulation-1h",
        "hype-passive-rsi-full-5m",
        "hype-passive-rsi-staged-5m",
    }
    assert all(item["risk_limits"]["max_drawdown"] == -0.08 for item in makers)
    assert all(
        item["risk_controls"]["per_position_stop"]["take_profit"] for item in makers
    )
    assert all(
        item["research_evidence"]["recent_120_day_replay"][
            "return_after_fees_and_slippage"
        ]
        > 0
        for item in makers
    )


# Live-driver window per starter (strategy warmup_bars + 20-bar margin).
# The driver windows the handed view to lookback_bars, capping ctx.bar_index;
# any starter whose warmup gate exceeds the window silently never trades —
# 7 of 12 entries did exactly that under the old 200-bar driver default.
# A new/edited starter MUST update this table consciously.
EXPECTED_STARTER_LOOKBACK_BARS = {
    "mixed-factor-balance-4h": 66,
    "mixed-rsi-snapback-1h": 224,
    "mixed-bollinger-pullback-1h": 224,
    "mixed-volume-capitulation-1h": 224,
    "balanced-passive-capitulation-1h": 224,
    "mixed-momentum-rank-1h": 360,
    "crypto-momentum-persistence-4h": 192,
    "mixed-sleeve-momentum-15m": 2904,
    "mixed-low-vol-rank-15m": 504,
    "hype-passive-rsi-full-5m": 38,
    "hype-passive-rsi-staged-5m": 38,
    "btc-eth-relative-strength-1d": 114,
    "bch-ltc-relative-strength-1d": 114,
}


def test_every_starter_lookback_clears_its_warmup_gate() -> None:
    assert set(EXPECTED_STARTER_LOOKBACK_BARS) == {
        definition.id for definition in STARTER_DEFINITIONS
    }
    for definition in STARTER_DEFINITIONS:
        # Independently rebuild the strategy the way the driver does and read
        # its warmup gate — the helpers must agree with the real strategy.
        module = importlib.import_module(definition.module)
        strategy = module.build_strategy(
            {**definition.configured_params(), "symbols": list(definition.symbols)}
        )
        warmup = int(strategy.warmup_bars)
        lookback = starter_lookback_bars(definition)
        assert warmup > 0, definition.id
        assert lookback > warmup, definition.id
        assert starter_warmup_bars(definition) == warmup, definition.id
        assert lookback == EXPECTED_STARTER_LOOKBACK_BARS[definition.id], definition.id


def test_starter_catalog_params_expose_lookback_bars() -> None:
    for item in starter_catalog():
        assert (
            item["params"]["lookback_bars"]
            == EXPECTED_STARTER_LOOKBACK_BARS[item["id"]]
        )


def test_create_starter_sets_driver_lookback_above_warmup(tmp_path) -> None:
    store = JobStore(repo_root=tmp_path)
    create_starter_job("mixed-sleeve-momentum-15m", store=store, compile_job=False)
    job = store.load("mixed-sleeve-momentum-15m")
    lookback = job.execution_params["lookback_bars"]
    assert lookback == 2904
    strategy = MixedSleeveMomentumStrategy(dict(job.execution_params))
    assert lookback > strategy.warmup_bars  # 2884: momentum_bars 2880 + 4


def test_buffered_rank_weights_keep_exact_basket_size() -> None:
    scores = {"A": 6.0, "B": 5.0, "C": 4.0, "D": 3.0, "E": 2.0, "F": 1.0}

    long_only = buffered_rank_weights(
        scores,
        {"C": 1.0},
        side_count=2,
        gross=0.60,
        long_only=True,
    )
    long_short = buffered_rank_weights(
        scores,
        {"C": 1.0, "D": -1.0},
        side_count=2,
        gross=0.80,
    )

    assert {symbol for symbol, weight in long_only.items() if weight > 0} == {"A", "C"}
    assert sum(weight > 0 for weight in long_short.values()) == 2
    assert sum(weight < 0 for weight in long_short.values()) == 2
    assert sum(abs(weight) for weight in long_short.values()) == pytest.approx(0.80)


def test_factor_balance_emits_only_scoped_long_sleeves() -> None:
    crypto = ["A", "B", "C", "D"]
    onchain = ["E", "F", "G", "H"]
    strategy = MixedFactorBalanceStrategy(
        {
            "symbols": [*crypto, *onchain],
            "sleeves": {"crypto": crypto, "onchain": onchain},
            "rebalance_bars": {"crypto": 1, "onchain": 1},
            "min_trade_notional": 0.0,
        }
    )
    strategy.warmup_bars = 1
    timestamp = pd.Timestamp("2026-01-01T00:00:00Z")
    rows = [
        {
            "timestamp": timestamp,
            "symbol": symbol,
            "open": 10.0,
            "high": 10.1,
            "low": 9.9,
            "close": 10.0,
            "volume": 1_000_000.0,
            "starter_factor_score": float(index),
            "starter_factor_scale": 1.0,
            "starter_stop_atr": 0.1,
        }
        for index, symbol in enumerate([*crypto, *onchain])
    ]
    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = "4h"
    ctx = ExecutionContext(
        view=CompletedBarsView.from_rows(rows),
        ledger=PositionLedger(),
        state_snapshot=StateSnapshot(status="valid"),
        capacity=None,
        params={"initial_capital": 10_000.0},
        timestamp=timestamp.isoformat(),
        execution_spec=spec,
    )

    intents = strategy.decide(ctx)

    assert {intent["symbol"] for intent in intents} == {"C", "D", "G", "H"}
    assert {intent["side"] for intent in intents} == {"buy"}
    notionals = {intent["symbol"]: intent["notional"] for intent in intents}
    assert notionals == pytest.approx(
        {"C": 2_000.0, "D": 2_000.0, "G": 2_500.0, "H": 2_500.0}
    )


def test_factor_balance_precompute_is_causal() -> None:
    symbols = ["BTC", "ETH", "SOL", "HYPE", "DOGE", "AAVE", "NEAR", "ENA"]
    timestamps = pd.date_range("2026-01-01", periods=70, freq="4h", tz="UTC")
    frames: dict[str, pd.DataFrame] = {}
    for symbol_index, symbol in enumerate(symbols):
        closes = [100.0 + symbol_index]
        for bar in range(1, len(timestamps)):
            change = 0.001 + 0.0002 * ((bar + symbol_index) % 5 - 2)
            closes.append(closes[-1] * (1.0 + change))
        frames[symbol] = pd.DataFrame(
            {
                "timestamp": timestamps,
                "symbol": symbol,
                "open": closes,
                "high": [value * 1.001 for value in closes],
                "low": [value * 0.999 for value in closes],
                "close": closes,
                "volume": 1_000_000.0,
                "funding": 0.00001 * (symbol_index + 1),
            }
        )
    strategy = MixedFactorBalanceStrategy(
        {
            "symbols": symbols,
            "sleeves": {"crypto": symbols},
            "min_assets": len(symbols),
        }
    )
    baseline = strategy.precompute(frames)
    changed = {symbol: frame.copy() for symbol, frame in frames.items()}
    changed["HYPE"].loc[changed["HYPE"].index[-1], "close"] *= 10.0

    mutated = strategy.precompute(changed)

    for symbol in symbols:
        pd.testing.assert_series_equal(
            baseline[symbol]["starter_factor_score"].iloc[:-1],
            mutated[symbol]["starter_factor_score"].iloc[:-1],
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


def test_crypto_momentum_concentrates_in_risk_adjusted_extremes() -> None:
    strategy = CryptoMomentumPersistenceStrategy(
        {
            "symbols": ["BTC", "ETH", "SOL", "HYPE"],
            "fast_momentum_bars": 2,
            "slow_momentum_bars": 4,
            "score_volatility_bars": 4,
            "rebalance_bars": 1,
            "min_trade_notional": 0.0,
        }
    )
    ctx = _context(
        strategy,
        {
            "BTC": [1.0, 0.98, 0.95, 0.92, 0.88, 0.83, 0.78, 0.72],
            "ETH": [1.0, 1.00, 1.01, 1.02, 1.03, 1.04, 1.05, 1.06],
            "SOL": [1.0, 1.01, 1.03, 1.06, 1.10, 1.15, 1.21, 1.28],
            "HYPE": [1.0, 1.05, 1.12, 1.22, 1.35, 1.52, 1.72, 1.95],
        },
        interval="4h",
    )

    intents = strategy.decide(ctx)

    hype = ctx.view.symbol_frame("HYPE")
    hype_close = pd.Series([1.0, 1.05, 1.12, 1.22, 1.35, 1.52, 1.72, 1.95])
    raw_score = 0.5 * (1.95 / 1.52 - 1.0) + 0.5 * (1.95 / 1.22 - 1.0)
    expected_score = raw_score / hype_close.pct_change().rolling(4).std().iloc[-1]
    assert hype["starter_momentum"].iloc[-1] == pytest.approx(expected_score)
    assert _sides(intents) == {"BTC": "sell", "ETH": "buy"}
    assert {intent["notional"] for intent in intents} == {3500.0}


def test_crypto_momentum_leans_long_without_raising_gross_in_broad_rally() -> None:
    strategy = CryptoMomentumPersistenceStrategy(
        {
            "symbols": ["BTC", "ETH", "SOL", "HYPE"],
            "fast_momentum_bars": 2,
            "slow_momentum_bars": 4,
            "score_volatility_bars": 4,
            "rebalance_bars": 1,
            "min_trade_notional": 0.0,
        }
    )
    ctx = _context(
        strategy,
        {
            "BTC": [1.00, 1.02, 1.04, 1.07, 1.10, 1.14, 1.18, 1.23],
            "ETH": [1.00, 1.03, 1.06, 1.10, 1.15, 1.21, 1.28, 1.36],
            "SOL": [1.00, 1.04, 1.08, 1.13, 1.19, 1.26, 1.34, 1.43],
            "HYPE": [1.00, 1.05, 1.11, 1.18, 1.26, 1.35, 1.45, 1.56],
        },
        interval="4h",
    )

    intents = strategy.decide(ctx)

    notionals = {intent["side"]: intent["notional"] for intent in intents}
    assert notionals == pytest.approx({"sell": 1750.0, "buy": 5250.0})
    assert sum(notionals.values()) == pytest.approx(7000.0)
    for symbol in ("BTC", "ETH", "SOL", "HYPE"):
        assert bool(ctx.view.symbol_frame(symbol)["gate_broad_bull"].iloc[-1])


def test_crypto_momentum_breadth_requires_timestamp_synchronized_symbols() -> None:
    strategy = CryptoMomentumPersistenceStrategy(
        {
            "symbols": ["A", "B"],
            "fast_momentum_bars": 1,
            "slow_momentum_bars": 1,
            "score_volatility_bars": 1,
            "broad_bull_momentum_threshold": 0.0,
            "stop_atr_period": 1,
        }
    )

    def frame(timestamps: list[str], closes: list[float]) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "timestamp": pd.to_datetime(timestamps, utc=True),
                "symbol": "A",
                "open": closes,
                "high": closes,
                "low": closes,
                "close": closes,
                "volume": 1.0,
            }
        )

    frames = {
        "A": frame(
            [
                "2026-01-01T00:00:00Z",
                "2026-01-01T01:00:00Z",
                "2026-01-01T02:00:00Z",
            ],
            [1.0, 1.1, 1.2],
        ),
        "B": frame(
            ["2026-01-01T00:00:00Z", "2026-01-01T02:00:00Z"],
            [1.0, 1.2],
        ),
    }
    frames["B"]["symbol"] = "B"

    derived = strategy.precompute(frames)
    assert bool(derived["A"]["gate_broad_bull"].iloc[1]) is False
    assert bool(derived["A"]["gate_broad_bull"].iloc[2]) is True


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


def test_volume_capitulation_can_rest_maker_only_entries() -> None:
    strategy = MixedVolumeCapitulationStrategy(
        {
            "symbols": ["A"],
            "rsi_period": 2,
            "entry_rsi": 101.0,
            "exit_rsi": 101.0,
            "trend_sma_period": 3,
            "volume_median_bars": 3,
            "min_trade_notional": 0.0,
            "entry_order_type": "maker",
            "entry_offset_atr": 0.25,
            "entry_ttl_bars": 2,
            "symbol_weights": {"A": 0.4},
        }
    )
    ctx = _context(
        strategy,
        {"A": [1.0, 1.0, 1.0, 2.0, 2.1, 2.0, 2.2]},
        volumes={"A": [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 2.0]},
        interval="1h",
    )

    entry = strategy.decide(ctx)[0]

    assert entry["action"] == "OPEN"
    assert entry["time_in_force"] == "ALO"
    assert entry["expires_after_bars"] == 2
    assert entry["limit_price"] < 2.2
    assert entry["notional"] == 4_000.0
    assert entry["bracket"]["cooldown_seconds"] == 86_400

    ctx.ledger.positions["A"] = PositionRecord(
        symbol="A", side="long", size=100.0, avg_price=2.0
    )
    assert strategy.decide(ctx) == []


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
        leverage=3,
    )
    job = store.load("my-starter")
    assert job.execution_contract == "jobs_v1"
    assert job.script_loop.mode == "paper"
    assert job.controller["starter"]["paper_only"] is True
    assert job.controller["starter"]["risk_limits"]["max_drawdown"] == -0.20
    assert job.controller["initializer_session_id"] == "ses_strategy-labbad"
    assert job.controller["starter"]["selected_leverage"] == 3
    assert job.execution_params["leverage"] == 3
    assert job.execution_params["lookback_bars"] == 360  # momentum_bars 336 + 4 + 20
    assert result["selected_leverage"] == 3
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
    evidence = json.loads(
        (store.job_dir(job.id) / "results/backtest/starter_evidence.json").read_text()
    )
    assert evidence["selected_leverage"] == 3
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

    maker_result = create_starter_job(
        "hype-passive-rsi-staged-5m",
        job_id="maker-starter",
        store=store,
        compile_job=False,
    )
    maker_job = store.load("maker-starter")
    assert maker_job.script_loop.interval_seconds == 300
    assert maker_job.execution_spec["data_contract"]["bar_interval"] == "5m"
    assert maker_job.execution_params["maker_fee_bps"] == 1.5
    assert maker_job.execution_params["maker_trade_through_bps"] == 1.0
    assert maker_job.controller["starter"]["risk_limits"]["max_drawdown"] == -0.08
    assert "hype_passive_rsi" in Path(maker_result["script_entrypoint"]).read_text(
        encoding="utf-8"
    )

    balanced_result = create_starter_job(
        "balanced-passive-capitulation-1h",
        job_id="balanced-maker-starter",
        store=store,
        compile_job=False,
    )
    balanced_job = store.load("balanced-maker-starter")
    assert balanced_job.execution_spec["data_contract"]["symbols"] == [
        "HYPE",
        "xyz:COIN",
        "xyz:TSLA",
    ]
    assert balanced_job.execution_params["entry_order_type"] == "maker"
    assert balanced_job.execution_params["symbol_weights"] == {
        "HYPE": 0.5,
        "xyz:COIN": 0.25,
        "xyz:TSLA": 0.25,
    }
    assert "mixed_volume_capitulation" in Path(
        balanced_result["script_entrypoint"]
    ).read_text(encoding="utf-8")


# Catalog launch policy: every off-the-shelf starter launches with the agent
# loop ON in intervene mode. Fleet evidence: the intervene copy of a starter
# was the fleet's only productive research job; its monitor twin burned ~49
# wakes/48h unable to act on a holdout-confirmed hypothesis; agent-off copies
# did zero research. A starter must never launch monitor/off by default.
def test_every_starter_launches_with_agent_loop_intervene(tmp_path) -> None:
    store = JobStore(repo_root=tmp_path)
    for definition in STARTER_DEFINITIONS:
        create_starter_job(definition.id, store=store, compile_job=False)
        job = store.load(definition.id)
        assert job.agent_loop.enabled is True, definition.id
        assert job.agent_loop.mode == "intervene", definition.id
        assert job.agent_loop.wake_interval_seconds == STARTER_AGENT_WAKE_SECONDS, (
            definition.id
        )
        assert job.job_kind == "script_agent", definition.id
        assert (
            job.controller["starter"]["catalog_version"] == STARTER_CATALOG_VERSION
        ), definition.id


def test_create_starter_honors_explicit_agent_mode_override(tmp_path) -> None:
    store = JobStore(repo_root=tmp_path)
    create_starter_job(
        "mixed-rsi-snapback-1h",
        job_id="monitor-override",
        store=store,
        compile_job=False,
        agent_mode="monitor",
    )
    monitor_job = store.load("monitor-override")
    assert monitor_job.agent_loop.mode == "monitor"
    assert monitor_job.agent_loop.enabled is True

    create_starter_job(
        "mixed-rsi-snapback-1h",
        job_id="off-override",
        store=store,
        compile_job=False,
        agent_mode="off",
    )
    off_job = store.load("off-override")
    assert off_job.agent_loop.mode == "off"
    assert off_job.agent_loop.enabled is False
    assert off_job.job_kind == "script_only"

    # Aliases normalize the same way as the generic create path.
    create_starter_job(
        "mixed-rsi-snapback-1h",
        job_id="improve-alias",
        store=store,
        compile_job=False,
        agent_mode="improve",
    )
    assert store.load("improve-alias").agent_loop.mode == "intervene"


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
    assert second["selected_leverage"] == 1
    assert second["leverage_warning"] is None


def test_create_starter_reuse_tolerates_out_of_range_leverage(tmp_path) -> None:
    """Reopen must never brick on a job whose recorded leverage drifted
    outside the starter dial (hand edit, governance clamp): clamp + warn."""
    store = JobStore(repo_root=tmp_path)
    create_starter_job("mixed-rsi-snapback-1h", store=store, compile_job=False)
    job = store.load("mixed-rsi-snapback-1h")
    job.execution_params["leverage"] = 7
    store.save(job)

    reused = create_starter_job("mixed-rsi-snapback-1h", store=store, compile_job=False)

    assert reused["created"] is False
    assert reused["selected_leverage"] == 5
    assert "clamped to 5" in reused["leverage_warning"]


def test_create_starter_spawns_detached_dataset_fetch(
    tmp_path, dataset_fetch_spawns
) -> None:
    store = JobStore(repo_root=tmp_path)
    result = create_starter_job("mixed-rsi-snapback-1h", store=store, compile_job=False)

    assert dataset_fetch_spawns == [
        {
            "job_id": "mixed-rsi-snapback-1h",
            "op": "fetch_dataset",
            "kwargs": {
                "job_id": "mixed-rsi-snapback-1h",
                "days": 120,
                "exchange": "hyperliquid",
                "quote": "USDC",
                "include_funding": True,
            },
        }
    ]
    assert result["dataset_fetch"] == {"spawned": True, "days": 120, "pid": 4242}
    events = _journal_events(store, "mixed-rsi-snapback-1h")
    spawned = next(
        event for event in events if event["type"] == "starter_dataset_fetch_spawned"
    )
    assert spawned["days"] == 120
    assert spawned["op"] == "fetch_dataset"
    assert spawned["ts"]

    reused = create_starter_job("mixed-rsi-snapback-1h", store=store, compile_job=False)
    assert reused["created"] is False
    assert len(dataset_fetch_spawns) == 1


def test_create_starter_skips_dataset_fetch_when_bars_exist(
    tmp_path, dataset_fetch_spawns
) -> None:
    store = JobStore(repo_root=tmp_path)
    bars_path = (
        store.job_dir("mixed-rsi-snapback-1h")
        / "results"
        / "backtest"
        / "input_bars.json"
    )
    bars_path.parent.mkdir(parents=True)
    bars_path.write_text("[]", encoding="utf-8")

    result = create_starter_job("mixed-rsi-snapback-1h", store=store, compile_job=False)

    assert result["created"] is True
    assert result["dataset_fetch"] == {"spawned": False, "reason": "dataset_exists"}
    assert dataset_fetch_spawns == []
    skipped = next(
        event
        for event in _journal_events(store, "mixed-rsi-snapback-1h")
        if event["type"] == "starter_dataset_fetch_skipped"
    )
    assert skipped["reason"] == "dataset_exists"


def test_create_starter_skips_dataset_fetch_when_op_already_running(
    tmp_path, monkeypatch
) -> None:
    store = JobStore(repo_root=tmp_path)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.starters.spawn_detached_op",
        lambda store, job_id, op, kwargs: {
            "already_running": True,
            "op": op,
            "job_id": job_id,
            "state": "running",
            "pid": 999,
        },
    )

    result = create_starter_job("mixed-rsi-snapback-1h", store=store, compile_job=False)

    assert result["created"] is True
    assert result["dataset_fetch"] == {
        "spawned": False,
        "reason": "fetch_already_running",
    }
    skipped = next(
        event
        for event in _journal_events(store, "mixed-rsi-snapback-1h")
        if event["type"] == "starter_dataset_fetch_skipped"
    )
    assert skipped["reason"] == "fetch_already_running"


def test_create_starter_survives_dataset_fetch_spawn_failure(
    tmp_path, monkeypatch
) -> None:
    store = JobStore(repo_root=tmp_path)

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("no child processes")

    monkeypatch.setattr("wayfinder_paths.jobs.starters.spawn_detached_op", boom)

    result = create_starter_job("mixed-rsi-snapback-1h", store=store, compile_job=False)

    assert result["created"] is True
    assert result["dataset_fetch"] == {"spawned": False, "error": "no child processes"}
    failed = next(
        event
        for event in _journal_events(store, "mixed-rsi-snapback-1h")
        if event["type"] == "starter_dataset_fetch_spawn_failed"
    )
    assert failed["error"] == "no child processes"


def test_missing_bars_error_reports_in_progress_dataset_fetch(tmp_path) -> None:
    store = JobStore(repo_root=tmp_path)
    create_starter_job("mixed-rsi-snapback-1h", store=store, compile_job=False)
    root = store.job_dir("mixed-rsi-snapback-1h")
    spec = ExecutionSpec()

    with pytest.raises(FileNotFoundError) as bare:
        _resolve_dataset(root, spec, {})
    assert "dataset fetch is in progress" not in str(bare.value)

    ops_dir = root / "state" / "background_ops"
    ops_dir.mkdir(parents=True)
    status_path = ops_dir / "fetch_dataset.json"
    status_path.write_text(
        json.dumps({"op": "fetch_dataset", "state": "running", "pid": os.getpid()}),
        encoding="utf-8",
    )
    with pytest.raises(
        FileNotFoundError, match="dataset fetch is in progress; retry shortly"
    ):
        _resolve_dataset(root, spec, {})

    # A stale status file from a dead child must not claim progress.
    status_path.write_text(
        json.dumps({"op": "fetch_dataset", "state": "running", "pid": 2**30}),
        encoding="utf-8",
    )
    with pytest.raises(FileNotFoundError) as stale:
        _resolve_dataset(root, spec, {})
    assert "dataset fetch is in progress" not in str(stale.value)


class _DownRunnerBridge:
    """Runner unreachable — snapshot degrades to the declared scorecard."""

    def __init__(self, *, repo_root=None):  # noqa: ANN001
        pass

    def job_states(self) -> dict[str, Any]:
        return {}


def _snapshot_scorecard(store: JobStore, job_id: str, monkeypatch) -> dict[str, Any]:
    monkeypatch.setattr(sync_mod, "RunnerBridge", _DownRunnerBridge)
    return snapshot_job(job_id, store=store)["scorecard"]


def _write_fetch_status(store: JobStore, job_id: str, status: dict[str, Any]) -> Path:
    ops_dir = store.job_dir(job_id) / "state" / "background_ops"
    ops_dir.mkdir(parents=True, exist_ok=True)
    (ops_dir / "fetch_dataset.json").write_text(json.dumps(status), encoding="utf-8")
    return ops_dir


def test_snapshot_reports_dataset_needed_before_fetch_op_exists(
    tmp_path, monkeypatch
) -> None:
    # Spawn is stubbed by the autouse fixture, so no op status file exists —
    # exactly the window between create and the child writing its status.
    store = JobStore(repo_root=tmp_path)
    create_starter_job("mixed-rsi-snapback-1h", store=store, compile_job=False)
    scorecard = _snapshot_scorecard(store, "mixed-rsi-snapback-1h", monkeypatch)
    assert scorecard["dataset_fetch"] == {"status": "needed"}


def test_snapshot_reports_running_dataset_fetch(tmp_path, monkeypatch) -> None:
    store = JobStore(repo_root=tmp_path)
    create_starter_job("mixed-rsi-snapback-1h", store=store, compile_job=False)
    _write_fetch_status(
        store,
        "mixed-rsi-snapback-1h",
        {
            "op": "fetch_dataset",
            "state": "running",
            "pid": os.getpid(),
            "started_at": "2026-08-19T00:00:00+00:00",
        },
    )
    scorecard = _snapshot_scorecard(store, "mixed-rsi-snapback-1h", monkeypatch)
    assert scorecard["dataset_fetch"] == {
        "status": "running",
        "started_at": "2026-08-19T00:00:00+00:00",
    }


@pytest.mark.parametrize(
    ("state", "expected"),
    [("done", "done"), ("failed", "failed"), ("killed", "failed"), ("lost", "failed")],
)
def test_snapshot_reports_finished_dataset_fetch(
    tmp_path, monkeypatch, state: str, expected: str
) -> None:
    store = JobStore(repo_root=tmp_path)
    create_starter_job("mixed-rsi-snapback-1h", store=store, compile_job=False)
    _write_fetch_status(
        store,
        "mixed-rsi-snapback-1h",
        {
            "op": "fetch_dataset",
            "state": state,
            "pid": 4242,
            "started_at": "2026-08-19T00:00:00+00:00",
            "finished_at": "2026-08-19T00:02:30+00:00",
        },
    )
    scorecard = _snapshot_scorecard(store, "mixed-rsi-snapback-1h", monkeypatch)
    assert scorecard["dataset_fetch"] == {
        "status": expected,
        "started_at": "2026-08-19T00:00:00+00:00",
        "finished_at": "2026-08-19T00:02:30+00:00",
    }


def test_snapshot_resolves_stale_running_fetch_via_result_file(
    tmp_path, monkeypatch
) -> None:
    # A `running` status from a dead child means the reaper is gone: a
    # parseable result file proves the detached fetch finished; without one
    # the run is lost and reported failed.
    store = JobStore(repo_root=tmp_path)
    create_starter_job("mixed-rsi-snapback-1h", store=store, compile_job=False)
    ops_dir = _write_fetch_status(
        store,
        "mixed-rsi-snapback-1h",
        {"op": "fetch_dataset", "state": "running", "pid": 2**30},
    )
    scorecard = _snapshot_scorecard(store, "mixed-rsi-snapback-1h", monkeypatch)
    assert scorecard["dataset_fetch"] == {"status": "failed"}

    (ops_dir / "fetch_dataset.result.json").write_text("{}", encoding="utf-8")
    scorecard = _snapshot_scorecard(store, "mixed-rsi-snapback-1h", monkeypatch)
    assert scorecard["dataset_fetch"] == {"status": "done"}


def test_snapshot_omits_dataset_fetch_when_bars_exist_and_no_op(
    tmp_path, monkeypatch
) -> None:
    # The common case — dataset present, nothing in flight — must not grow a
    # key: existing snapshots stay byte-identical.
    store = JobStore(repo_root=tmp_path)
    create_starter_job("mixed-rsi-snapback-1h", store=store, compile_job=False)
    bars_path = (
        store.job_dir("mixed-rsi-snapback-1h")
        / "results"
        / "backtest"
        / "input_bars.json"
    )
    bars_path.write_text("[]", encoding="utf-8")
    scorecard = _snapshot_scorecard(store, "mixed-rsi-snapback-1h", monkeypatch)
    assert "dataset_fetch" not in scorecard


def test_snapshot_omits_dataset_fetch_for_ordinary_jobs(tmp_path, monkeypatch) -> None:
    # A non-starter job without bars has no evidence file — "needed" is a
    # starter-only signal, ordinary jobs keep their snapshot unchanged.
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "carry", script="strategy.py", interval_seconds=3600, agent_mode="monitor"
    )
    store.create_job(job)
    scorecard = _snapshot_scorecard(store, job.id, monkeypatch)
    assert "dataset_fetch" not in scorecard


@pytest.mark.parametrize(
    ("value", "expected", "warns"),
    [
        (3, 3, False),
        (0, 1, True),
        (7, 5, True),
        (2.4, 2, True),
        ("five", 1, True),
        (float("nan"), 1, True),
        (None, 1, True),
    ],
)
def test_coerce_starter_leverage_clamps_instead_of_raising(
    value, expected, warns
) -> None:
    coerced, warning = coerce_starter_leverage(value)
    assert coerced == expected
    assert (warning is not None) is warns


@pytest.mark.parametrize("value", [0, 6, 1.5, True, "five", float("nan")])
def test_starter_leverage_rejects_values_outside_discrete_dial(value) -> None:
    with pytest.raises(ValueError, match="whole number from 1 to 5"):
        validate_starter_leverage(value)


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
