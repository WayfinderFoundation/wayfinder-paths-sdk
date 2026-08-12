"""Tiered evidence: regime classifier, per-regime scan rows, recency
diagnostics, and the probation verdict — Tier-1 promote gates unchanged."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from wayfinder_paths.jobs.indicators import (
    REGIME_LABELS,
    classify_regimes,
    current_regime,
)
from wayfinder_paths.jobs.research import apply_bh_verdicts, scan_signals
from wayfinder_paths.jobs.workspace_signals import SignalDef


def _frame(count: int = 800, *, trend_break: int | None = None) -> pd.DataFrame:
    base = pd.Timestamp("2026-06-01T00:00:00Z")
    rows = []
    price = 100.0
    rng = np.random.default_rng(7)
    for i in range(count):
        drift = 0.02 if (trend_break is not None and i >= trend_break) else -0.01
        price = max(1.0, price + drift + float(rng.normal(0, 0.15)))
        rows.append(
            {
                "timestamp": (base + pd.Timedelta(minutes=5 * i)).isoformat(),
                "symbol": "LIT",
                "open": price,
                "high": price + 0.3,
                "low": price - 0.3,
                "close": price,
                "volume": 10.0,
            }
        )
    return pd.DataFrame(rows)


def test_regime_classifier_causal_and_covering() -> None:
    frame = _frame(600, trend_break=300)
    labels = classify_regimes(frame)
    known = labels.dropna()
    assert set(known.unique()) <= set(REGIME_LABELS)
    assert {lab.split("_")[0] for lab in known.unique()} == {"up", "down"}
    # Prefix property: appending future bars never changes earlier labels.
    prefix = classify_regimes(frame.iloc[:-50].reset_index(drop=True))
    pd.testing.assert_series_equal(
        prefix.iloc[100:200], labels.iloc[100:200], check_names=False
    )
    assert current_regime(frame) in REGIME_LABELS


def test_scan_emits_regime_rows_and_recency_fields() -> None:
    frame = _frame(900)
    extra = [
        SignalDef(
            name="probe_dip",
            family="test",
            description="below 20-bar mean",
            min_bars=20,
            build=lambda f: f["close"].astype(float)
            < f["close"].astype(float).rolling(20).mean(),
        )
    ]
    result = scan_signals(
        frame,
        bar_seconds=300,
        extra_signals=extra,
        include_canonical=False,
        condition_regime=True,
        min_events=20,
        holdout_fraction=0.1,
    )
    rows = result["_all_rows"]
    base_rows = [r for r in rows if not r.get("regime")]
    regime_rows = [r for r in rows if r.get("regime")]
    assert base_rows and regime_rows
    assert result["current_regime"] in REGIME_LABELS
    assert all(r["regime"] in REGIME_LABELS for r in regime_rows)
    assert any(
        r["in_current_regime"] == (r["regime"] == result["current_regime"])
        for r in regime_rows
    )
    for r in base_rows:
        assert "recency_trend" in r and "t_recent" in r


def _verdict_row(**overrides: object) -> dict:
    t = float(overrides.get("t_stat_vs_drift", 3.0))
    base = {
        "t_stat_vs_drift": t,
        "p_value": 2 * (1 - 0.9999) if abs(t) > 3 else 0.05,
        "fold_stable": False,
        "folds_agreeing": 2,
        "n": 40,
        "t_recent": 2.5,
    }
    base.update(overrides)
    return base


def _p(t: float) -> float:
    return 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(t) / math.sqrt(2.0))))


def test_probation_verdict_paths_and_tier1_unchanged() -> None:
    # One strong row (full gates) + one near-miss + fillers so BH q-values
    # spread realistically.
    rows = [
        _verdict_row(  # Tier-1: unchanged full promote
            t_stat_vs_drift=4.5, p_value=_p(4.5), fold_stable=True, folds_agreeing=4
        ),
        _verdict_row(  # path (a): near-miss, alive now
            t_stat_vs_drift=2.8, p_value=_p(2.8), folds_agreeing=2, t_recent=2.4
        ),
        _verdict_row(  # near-miss but DEAD recently -> stays candidate
            t_stat_vs_drift=2.8, p_value=_p(2.8), folds_agreeing=2, t_recent=0.3
        ),
        _verdict_row(  # path (b): regime-conditional in current regime
            t_stat_vs_drift=3.0,
            p_value=_p(3.0),
            folds_agreeing=1,
            t_recent=None,
            regime="down_highvol",
            in_current_regime=True,
            n=25,
        ),
        _verdict_row(  # same but NOT the current regime -> candidate
            t_stat_vs_drift=3.0,
            p_value=_p(3.0),
            folds_agreeing=1,
            t_recent=None,
            regime="up_lowvol",
            in_current_regime=False,
            n=25,
        ),
        _verdict_row(  # path (c): recent-window survivor caps at probation
            t_stat_vs_drift=4.6,
            p_value=_p(4.6),
            fold_stable=True,
            folds_agreeing=4,
            window_days=60,
        ),
        _verdict_row(t_stat_vs_drift=1.0, p_value=_p(1.0), t_recent=None),
    ]
    apply_bh_verdicts(rows)
    verdicts = [r["verdict"] for r in rows]
    assert verdicts[0] == "promote"  # Tier-1 untouched
    assert verdicts[1] == "probation"  # alive near-miss
    assert verdicts[2] == "candidate"  # decayed near-miss
    assert verdicts[3] == "probation"  # current-regime conditional
    assert verdicts[4] == "candidate"  # wrong-regime conditional
    assert verdicts[5] == "probation"  # window survivor never Tier-1
    assert verdicts[6] is None


def test_probation_registry_lifecycle(tmp_path) -> None:
    import pytest

    from wayfinder_paths.jobs.models import WayfinderJob
    from wayfinder_paths.jobs.probation import (
        load_probation,
        record_probation_leg,
        update_probation_leg,
    )
    from wayfinder_paths.jobs.store import JobStore
    from wayfinder_paths.jobs.sync import snapshot_job

    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("prob-demo", agent_mode="intervene")
    store.save(job)

    record_probation_leg(
        store,
        job.id,
        name="lit_dt_short",
        symbol="LIT",
        size_fraction=0.5,
        graduate_criterion=">=15 trades WR 45-65%",
        kill_criterion="<=-250bps or regime flip",
        proposal_id="prop-x",
    )
    with pytest.raises(ValueError, match="already exists"):
        record_probation_leg(
            store,
            job.id,
            name="lit_dt_short",
            symbol="LIT",
            size_fraction=0.4,
            graduate_criterion="g",
            kill_criterion="k",
        )
    with pytest.raises(ValueError, match="size_fraction"):
        record_probation_leg(
            store,
            job.id,
            name="oversize",
            symbol="POL",
            size_fraction=0.8,
            graduate_criterion="g",
            kill_criterion="k",
        )
    record_probation_leg(
        store,
        job.id,
        name="leg2",
        symbol="POL",
        size_fraction=0.3,
        graduate_criterion="g",
        kill_criterion="k",
    )
    with pytest.raises(ValueError, match="max 2 concurrent"):
        record_probation_leg(
            store,
            job.id,
            name="leg3",
            symbol="XRP",
            size_fraction=0.3,
            graduate_criterion="g",
            kill_criterion="k",
        )

    update_probation_leg(
        store,
        job.id,
        "lit_dt_short",
        progress="7/15 trades, WR 57%",
        kill_status="-80bps (ok)",
    )
    update_probation_leg(store, job.id, "leg2", status="killed")

    doc = load_probation(store, job.id)
    lit = next(leg for leg in doc["legs"] if leg["name"] == "lit_dt_short")
    assert lit["graduate"]["progress"] == "7/15 trades, WR 57%"
    assert (
        next(leg for leg in doc["legs"] if leg["name"] == "leg2")["status"] == "killed"
    )

    # Registry rides the snapshot (and therefore the backend sync + UI).
    snap = snapshot_job(job.id, store=store)
    assert len(snap["probation"]["legs"]) == 2

    journal = (store.job_dir(job.id) / "journal.jsonl").read_text()
    assert "probation_leg_opened" in journal
    assert "probation_leg_killed" in journal


def test_forward_view_trades_and_marker_directions(tmp_path) -> None:
    import json as _json

    from wayfinder_paths.jobs.forward_artifacts import load_forward_view
    from wayfinder_paths.jobs.models import WayfinderJob
    from wayfinder_paths.jobs.store import JobStore

    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("view-demo", agent_mode="intervene")
    store.save(job)
    forward = store.job_dir(job.id) / "results" / "forward"
    forward.mkdir(parents=True, exist_ok=True)
    fills = [
        {
            "symbol": "LIT",
            "side": "sell",
            "reduce_only": False,
            "status": "filled",
            "timestamp": "2026-07-22T15:05:00+00:00",
            "avg_price": 2.3308,
            "raw": {"intent_metadata": {"entry_reason": "fade"}},
        },
        {
            "symbol": "LIT",
            "side": "buy",
            "reduce_only": True,
            "status": "filled",
            "timestamp": "2026-07-22T16:05:00+00:00",
            "avg_price": 2.3908,
            "raw": {"intent_metadata": {"exit_reason": ""}},
        },
    ]
    (forward / "fills.jsonl").write_text(
        "\n".join(_json.dumps(f) for f in fills) + "\n", encoding="utf-8"
    )
    (forward / "trades.jsonl").write_text(
        _json.dumps(
            {
                "symbol": "LIT",
                "side": "buy",
                "price": 2.3908,
                "net_pnl": -0.644,
                "closed_at": "2026-07-22T16:05:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (forward / "ticks.jsonl").write_text("", encoding="utf-8")

    view = load_forward_view(job.id, store=store, include_prices=False)
    # Markers say LONG/SHORT, not buy/sell: a sell entry is a SHORT entry
    # and the buy that closes it is a SHORT exit.
    entry, exit_ = view["visualization"]["markers"]
    assert entry["direction"] == "short" and entry["kind"] == "entry"
    assert exit_["direction"] == "short" and exit_["kind"] == "exit"
    assert "short entry" in entry["label"]
    # Full trades list: entry-joined with direction and duration.
    trade = view["trades"][0]
    assert trade["direction"] == "short"
    assert trade["entry_price"] == 2.3308 and trade["exit_price"] == 2.3908
    assert trade["duration_minutes"] == 60
    assert trade["entry_reason"] == "fade"
    assert trade["exit_reason"] == "bracket_stop"


def test_regime_aware_aliveness_and_cell_promotion() -> None:
    """IMX calibration: dead-recency averages cannot promote; full-gate
    regime cells promote as regime-gated legs regardless of current regime."""
    rows = [
        _verdict_row(  # Tier-1 with healthy recency: promotes as before
            t_stat_vs_drift=4.5,
            p_value=_p(4.5),
            fold_stable=True,
            folds_agreeing=4,
            t_recent=2.0,
        ),
        _verdict_row(  # Tier-1 stats but recency SIGN-FLIPPED: blocked
            t_stat_vs_drift=4.4,
            p_value=_p(4.4),
            fold_stable=True,
            folds_agreeing=4,
            t_recent=-2.1,
        ),
        _verdict_row(  # Tier-1 with thin halves (t_recent None): not blocked
            t_stat_vs_drift=4.3,
            p_value=_p(4.3),
            fold_stable=True,
            folds_agreeing=4,
            t_recent=None,
        ),
        _verdict_row(  # full-gate REGIME cell, not current regime: promotes
            t_stat_vs_drift=4.2,
            p_value=_p(4.2),
            fold_stable=True,
            folds_agreeing=4,
            t_recent=2.2,
            regime="down_highvol",
            in_current_regime=False,
        ),
        _verdict_row(t_stat_vs_drift=0.5, p_value=_p(0.5), t_recent=None),
    ]
    apply_bh_verdicts(rows)
    assert rows[0]["verdict"] == "promote"
    assert rows[1]["verdict"] != "promote"  # dead average blocked
    assert rows[2]["verdict"] == "promote"  # thin halves do not block
    assert rows[3]["verdict"] == "promote"
    assert rows[3]["promote_scope"] == "regime"  # deployment must carry the gate
    assert "promote_scope" not in rows[0]


def test_walk_forward_recency_weighting() -> None:
    from wayfinder_paths.jobs.execution.walk_forward import _summary

    def fold(net: float) -> dict:
        stats = {
            "net_return": net,
            "sharpe": 1.0,
            "sortino": 1.0,
            "max_drawdown_pct": -0.01,
        }
        return {"status": "ok", "train_stats": dict(stats), "test_stats": dict(stats)}

    early_good = _summary(
        [fold(0.10), fold(0.05), fold(-0.02), fold(-0.04)], "net_return"
    )
    late_good = _summary(
        [fold(-0.04), fold(-0.02), fold(0.05), fold(0.10)], "net_return"
    )
    # Same raw mean; recency weighting must prefer the strategy that works NOW.
    assert abs(early_good["oos_return_mean"] - late_good["oos_return_mean"]) < 1e-12
    spread = (
        late_good["oos_return_recency_weighted"]
        - early_good["oos_return_recency_weighted"]
    )
    assert spread > 0.03  # decisive separation, not a rounding artifact
    assert late_good["oos_return_recency_weighted"] > late_good["oos_return_mean"]
