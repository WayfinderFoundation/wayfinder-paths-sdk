from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
import yaml

from wayfinder_paths.jobs.archive import record_candidate
from wayfinder_paths.jobs.defense import (
    OOD_ACTIVE_COLUMN,
    active_stand_down_symbols,
    add_defense_features,
    defense_feature_warmup_bars,
    defense_policy,
    record_stop_loss_result,
    scale_entry_intents,
)
from wayfinder_paths.jobs.evolution_campaign import (
    _campaign_regime_context,
    _freeze_research_context,
    _full_dev_verdict,
)
from wayfinder_paths.jobs.execution.primitives import CompletedBarsView, OrderIntent
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.probation import _paired_forward_metrics
from wayfinder_paths.jobs.regime import (
    MIXED_REGIME,
    add_portfolio_regime_feature,
    classify_portfolio_regimes,
    partition_regime_returns,
)
from wayfinder_paths.jobs.store import JobStore

SYMBOLS = ("SOL", "XRP", "POL", "HYPE")


def _panel_rows(*, count: int = 560, interval: timedelta = timedelta(hours=1)):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = []
    for index in range(count):
        for offset, symbol in enumerate(SYMBOLS):
            close = 100 + offset * 10 + index * 0.03 + np.sin(index / 9 + offset)
            rows.append(
                {
                    "timestamp": (start + index * interval).isoformat(),
                    "symbol": symbol,
                    "open": close - 0.05,
                    "high": close + 0.2,
                    "low": close - 0.2,
                    "close": close,
                    "volume": 1000.0,
                }
            )
    return rows


def test_portfolio_regime_is_causal_and_engine_owned() -> None:
    rows = _panel_rows()
    frame = pd.DataFrame(rows)
    original = classify_portfolio_regimes(frame, universe=SYMBOLS)
    cutoff = sorted(frame["timestamp"].unique())[520]
    mutated = frame.copy()
    mutated.loc[mutated["timestamp"] > cutoff, ["high", "close"]] *= 4
    mutated.loc[mutated["timestamp"] > cutoff, "low"] *= 0.2
    changed = classify_portfolio_regimes(mutated, universe=SYMBOLS)

    pd.testing.assert_series_equal(
        original.loc[: pd.Timestamp(cutoff)], changed.loc[: pd.Timestamp(cutoff)]
    )
    assert (original != MIXED_REGIME).any()

    view = CompletedBarsView.from_rows(rows)
    legacy = add_portfolio_regime_feature(view, {"symbols": list(SYMBOLS)})
    assert "__wf_portfolio_regime" not in legacy.to_frame().columns
    specialized = add_portfolio_regime_feature(
        view,
        {"symbols": list(SYMBOLS), "target_regimes": ["up_lowvol"]},
    )
    assert "__wf_portfolio_regime" in specialized.to_frame().columns


def test_regime_returns_follow_marked_equity_not_trade_exit_bucket() -> None:
    start = pd.Timestamp("2026-01-01T00:00:00Z")
    labels = {
        start: "up_lowvol",
        start + pd.Timedelta(days=1): "up_lowvol",
        start + pd.Timedelta(days=2): "down_highvol",
    }
    stats = partition_regime_returns(
        [
            {"timestamp": start, "equity": 100.0},
            {"timestamp": start + pd.Timedelta(days=1), "equity": 110.0},
            {"timestamp": start + pd.Timedelta(days=2), "equity": 99.0},
        ],
        [
            {
                "timestamp": start + pd.Timedelta(days=1),
                "reduce_only": False,
            }
        ],
        labels=labels,
        target_regimes=["up_lowvol"],
    )

    assert stats["target_net_return"] == pytest.approx(0.10)
    assert stats["outside_loss_pct"] == pytest.approx(0.10)
    assert stats["target_trade_count"] == 1


def test_breadth_shock_scales_entries_but_never_exits() -> None:
    assert defense_feature_warmup_bars(300) == 690
    rows = _panel_rows(count=720, interval=timedelta(minutes=5))
    for row in rows[-len(SYMBOLS) :]:
        row["close"] *= 1.5
        row["high"] = row["close"] * 1.001
    view = add_defense_features(
        CompletedBarsView.from_rows(rows),
        {"symbols": list(SYMBOLS), "defense_overlay": {}},
    )

    assert bool(view.feature(OOD_ACTIVE_COLUMN)) is True
    intents = [
        OrderIntent(
            action="OPEN",
            venue="paper",
            symbol="SOL",
            side="buy",
            notional=100.0,
        ),
        OrderIntent(
            action="STOP_LOSS",
            venue="paper",
            symbol="SOL",
            side="sell",
            size=2.0,
            reduce_only=False,
        ),
    ]
    assert scale_entry_intents(intents, 0.25) == 1
    assert intents[0].notional == pytest.approx(25.0)
    assert intents[1].size == pytest.approx(2.0)


def test_stop_loss_streak_persists_a_bounded_symbol_stand_down() -> None:
    state = {"policy": defense_policy({"defense_overlay": {}})}
    started = pd.Timestamp("2026-08-19T12:00:00Z")
    for index in range(3):
        event = record_stop_loss_result(
            state,
            symbol="HYPE",
            direction="short",
            realized_pnl=-5.0,
            timestamp=started + pd.Timedelta(hours=index),
        )

    assert event and event["kind"] == "loss_streak_symbol_stand_down"
    assert active_stand_down_symbols(state, now=started + pd.Timedelta(hours=3)) == {
        "HYPE"
    }
    assert not active_stand_down_symbols(state, now=started + pd.Timedelta(hours=15))


def test_profitable_close_breaks_the_loss_streak_but_ttl_losses_count() -> None:
    # TTL and explicit closes at a loss arm the breaker exactly like stops:
    # counting stop fills alone left it inert for time-capped strategies.
    state = {"policy": defense_policy({"defense_overlay": {}})}
    started = pd.Timestamp("2026-08-19T12:00:00Z")
    for offset in range(2):
        record_stop_loss_result(
            state,
            symbol="HYPE",
            direction="short",
            realized_pnl=-5.0,
            timestamp=started + pd.Timedelta(hours=offset),
        )
    record_stop_loss_result(
        state,
        symbol="HYPE",
        direction="short",
        realized_pnl=2.0,
        timestamp=started + pd.Timedelta(hours=2),
    )

    assert (
        record_stop_loss_result(
            state,
            symbol="HYPE",
            direction="short",
            realized_pnl=-5.0,
            timestamp=started + pd.Timedelta(hours=3),
        )
        is None
    )
    event = None
    for offset in range(4, 6):
        event = record_stop_loss_result(
            state,
            symbol="HYPE",
            direction="short",
            realized_pnl=-1.0,
            timestamp=started + pd.Timedelta(hours=offset),
        )
    assert event and event["streak"] == 3 and event["mode"] == "active"


def test_breadth_shock_stays_active_through_the_cooldown() -> None:
    # 720 calm 5m bars, then a level shift held flat for 30h: the 24h return
    # normalizes after a day, so only the cooldown keeps the scale reduced.
    rows = _panel_rows(count=720 + 360, interval=timedelta(minutes=5))
    for row in rows[720 * len(SYMBOLS) :]:
        row["close"] *= 1.5
        row["high"] = row["close"] * 1.001
        row["low"] = row["close"] * 0.999
    params = {"symbols": list(SYMBOLS), "defense_overlay": {}}

    assert bool(
        add_defense_features(CompletedBarsView.from_rows(rows), params).feature(
            OOD_ACTIVE_COLUMN
        )
    )
    params["defense_overlay"] = {"ood_cooldown_hours": 1}
    assert not bool(
        add_defense_features(CompletedBarsView.from_rows(rows), params).feature(
            OOD_ACTIVE_COLUMN
        )
    )


def test_full_dev_verdict_waits_for_absent_regime_and_codes_inversions() -> None:
    base = {
        "specialized": True,
        "valid": True,
        "validation_trades": 12,
        "minimum_validation_trades": 8,
        "train_return": 0.02,
        "validation_return": 0.01,
        "stress_return": 0.005,
        "outside_loss_ok": True,
        "target_days": 12,
        "min_target_days": 10,
        "audit_passed": True,
    }
    assert _full_dev_verdict(**base)["status"] == "dev_frontier"

    absent = _full_dev_verdict(**{**base, "target_days": 3})
    assert absent["status"] == "awaiting_regime"
    assert absent["failure_codes"] == []
    assert "not evidence against the idea" in absent["evidence"]

    inverted = _full_dev_verdict(
        **{**base, "validation_return": -0.01, "outside_loss_ok": False}
    )
    assert inverted["status"] == "low_fidelity_rejected"
    assert inverted["failure_codes"] == [
        "negative_in_target_regime",
        "screen_inversion",
        "out_of_regime_loss_budget",
    ]
    plain = _full_dev_verdict(**{**base, "specialized": False, "valid": False})
    assert plain["status"] == "low_fidelity_rejected"
    assert plain["failure_codes"] == []


def test_refutation_ledger_requires_evidence_against_the_family(tmp_path) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("regime-ledger", script="workspace/strategy.py")
    store.save(job)
    for candidate_id, family, status, metadata in (
        ("infra", "trend-breakout", "invalid", {}),
        ("floor", "trend-breakout", "low_fidelity_rejected", {}),
        ("parked", "trend-breakout", "awaiting_regime", {}),
        (
            "flip",
            "volatility-fade",
            "low_fidelity_rejected",
            {
                "full_dev_failure_codes": [
                    "negative_in_target_regime",
                    "screen_inversion",
                ]
            },
        ),
    ):
        record_candidate(
            store,
            job.id,
            candidate_id=candidate_id,
            family=family,
            summary=candidate_id,
            status=status,
            objective=None,
            evidence=candidate_id,
            metadata=metadata,
        )

    refuted = _freeze_research_context(store, job.id)["refuted_families"]

    assert [item["family"] for item in refuted] == ["volatility-fade"]
    assert refuted[0]["strength"] == "screen_inversion"


def test_campaign_regime_context_freezes_primary_and_counter(tmp_path) -> None:
    dataset = tmp_path / "input_bars.json"
    dataset.write_text(json.dumps({"bars": _panel_rows()}), encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    (source / "job.yaml").write_text(
        yaml.safe_dump({"execution_params": {"symbols": list(SYMBOLS)}}),
        encoding="utf-8",
    )

    context = _campaign_regime_context(dataset, source, enabled=True)

    assert context["available"] is True
    assert context["primary_regime"] != context["counter_regime"]
    assert set(context["universe"]) == set(SYMBOLS)


def test_forward_probation_uses_target_days_and_outside_loss_budget(tmp_path) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("regime-probation", script="workspace/strategy.py")
    store.save(job)
    root = store.job_dir(job.id)
    candidate = root / "results/forward/candidate"
    reference = root / "results/forward/reference"
    candidate.mkdir(parents=True)
    reference.mkdir(parents=True)
    started = datetime(2026, 8, 1, tzinfo=UTC)
    candidate_trades = []
    candidate_ticks = []
    reference_ticks = []
    for offset in range(7):
        stamp = started + timedelta(days=offset, hours=12)
        label = "up_highvol" if offset < 6 else "down_lowvol"
        tick = {
            "bar_ts": stamp.isoformat(),
            "gates": {"portfolio_regime": {"current": label}},
        }
        candidate_ticks.append(tick)
        reference_ticks.append({"bar_ts": stamp.isoformat()})
        candidate_trades.append(
            {
                "closed_at": stamp.isoformat(),
                "net_pnl": 10.0 if offset < 6 else -300.0,
            }
        )
    (candidate / "ticks.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in candidate_ticks),
        encoding="utf-8",
    )
    (reference / "ticks.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in reference_ticks),
        encoding="utf-8",
    )
    (candidate / "trades.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in candidate_trades),
        encoding="utf-8",
    )
    trial = {
        "forward": {"started_at": started.isoformat(), "confidence": 0.9},
        "candidate": {"stream": "results/forward/candidate"},
        "reference": {"stream": "results/forward/reference"},
        "regime_contract": {
            "target_regimes": ["up_highvol"],
            "outside_loss_budget_pct": 0.02,
        },
    }

    metrics = _paired_forward_metrics(
        store,
        job.id,
        trial,
        current=started + timedelta(days=8),
    )

    assert metrics["paired_days"] == 6
    assert metrics["outside_days"] == 1
    assert metrics["outside_loss_pct"] == pytest.approx(0.03)
    assert metrics["outside_loss_breach"] is True
    assert metrics["overall_estimate"] < 0
    assert metrics["target_days_short"] is True
