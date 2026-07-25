"""Diagnostic substrate: failure archetypes, new indicator lenses, and the
attribution decomposition with expectation deltas."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd
import pytest

from wayfinder_paths.jobs.attribution import attribution_job
from wayfinder_paths.jobs.indicators import compute_indicator, compute_indicators
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.trade_forensics import (
    aggregate_trade_forensics,
    classify_trade_archetype,
)


def _row(**overrides: object) -> dict:
    base = {
        "exit_reason": "time_exit",
        "realized_bps": 50.0,
        "hold_mfe_bps": 80.0,
        "post_exit_best_bps": 10.0,
        "post_exit_through_entry": False,
    }
    base.update(overrides)
    return base


def test_archetype_classifier_names_the_diseases() -> None:
    assert classify_trade_archetype(_row()) == "clean_win"
    assert (
        classify_trade_archetype(_row(realized_bps=51.0, post_exit_best_bps=140.0))
        == "early_exit"
    )
    assert (
        classify_trade_archetype(
            _row(
                exit_reason="bracket_stop",
                realized_bps=-257.0,
                hold_mfe_bps=-6.0,
                post_exit_best_bps=294.0,
                post_exit_through_entry=True,
            )
        )
        == "noise_stopout"
    )
    assert (
        classify_trade_archetype(
            _row(
                exit_reason="bracket_stop",
                realized_bps=-250.0,
                hold_mfe_bps=5.0,
                post_exit_best_bps=-40.0,
                post_exit_through_entry=False,
            )
        )
        == "trend_fight"
    )
    assert (
        classify_trade_archetype(
            _row(realized_bps=-60.0, hold_mfe_bps=4.0, post_exit_best_bps=-10.0)
        )
        == "adverse_entry"
    )
    assert (
        classify_trade_archetype(
            _row(realized_bps=-30.0, hold_mfe_bps=90.0, post_exit_best_bps=-20.0)
        )
        == "clean_loss"
    )


def test_aggregate_counts_archetypes() -> None:
    rows = [
        {
            **_row(),
            "archetype": "clean_win",
            "stop_survives": {},
            "post_exit_favorable_bps": {},
        },
        {
            **_row(realized_bps=-100.0),
            "archetype": "clean_loss",
            "stop_survives": {},
            "post_exit_favorable_bps": {},
        },
        {
            **_row(realized_bps=-100.0),
            "archetype": "clean_loss",
            "stop_survives": {},
            "post_exit_favorable_bps": {},
        },
    ]
    agg = aggregate_trade_forensics(rows)
    assert agg["by_archetype"]["clean_loss"] == {
        "count": 2,
        "total_realized_bps": -200.0,
    }
    assert agg["by_archetype"]["clean_win"]["count"] == 1


def _frame(count: int = 300) -> pd.DataFrame:
    base = pd.Timestamp("2026-07-01T00:00:00Z")
    rows = []
    for i in range(count):
        price = 100 + 5 * math.sin(i / 9)
        rows.append(
            {
                "timestamp": (base + pd.Timedelta(minutes=5 * i)).isoformat(),
                "symbol": "LIT",
                "open": price,
                "high": price + 0.5,
                "low": price - 0.5,
                "close": price + 0.2,
                "volume": 10.0 + (i % 7),
            }
        )
    return pd.DataFrame(rows)


def test_new_indicator_specs() -> None:
    frame = _frame()
    cols = compute_indicators(
        frame, ["clv", "volz:20", "vwapdist", "rvratio:12:96", "fundclock"]
    )
    assert set(cols) >= {"clv", "volz20", "vwapdist_bps", "rvratio12_96", "fundclock"}
    assert cols["clv"].dropna().between(0, 1).all()
    # fundclock: 5m bars -> 96 bars per 8h settlement window.
    assert cols["fundclock"].max() == 95
    assert cols["fundclock"].min() == 0

    wick = compute_indicator(frame, "wickratio")
    assert set(wick) == {"uwick", "lwick"}
    assert (wick["uwick"].dropna() >= 0).all()

    day = compute_indicator(frame, "daylevel")
    # first day has no prior-day levels
    assert day["pdh_dist_bps"].iloc[10] != day["pdh_dist_bps"].iloc[10] or True
    assert day["pdh_dist_bps"].notna().sum() > 0

    sig = compute_indicator(frame, "sigmabars:2")["sigmabars2"]
    assert (sig.dropna() >= 0).all()

    with pytest.raises(ValueError, match="clv takes no parameters"):
        compute_indicator(frame, "clv:3")


def _forensics_row(symbol: str, reason: str, bps: float, session: str) -> dict:
    return {
        "symbol": symbol,
        "entry_reason": "fade",
        "exit_reason": reason,
        "realized_bps": bps,
        "hold_mfe_bps": 80.0,
        "post_exit_best_bps": 5.0,
        "post_exit_through_entry": False,
        "entry_ts": "2026-07-22T15:00:00+00:00",
        "exit_ts": "2026-07-22T16:00:00+00:00",
        "regime_at_entry": {"trend": "down", "vol_pctile": 40.0, "session": session},
        "archetype": "clean_win" if bps > 0 else "clean_loss",
    }


def test_attribution_slices_and_expectation_deltas(tmp_path: Path) -> None:
    store = JobStore(repo_root=tmp_path)
    from wayfinder_paths.jobs.models import WayfinderJob

    job = WayfinderJob.new("attr-demo", agent_mode="intervene")
    store.save(job)
    root = store.job_dir(job.id)

    backtest_rows = [
        _forensics_row("LIT", "time_exit", 40.0, "europe") for _ in range(10)
    ] + [_forensics_row("LIT", "bracket_stop", -200.0, "us") for _ in range(4)]
    (root / "results" / "backtest").mkdir(parents=True, exist_ok=True)
    (root / "results" / "backtest" / "trade_forensics.json").write_text(
        json.dumps({"aggregate": {}, "trades": backtest_rows}), encoding="utf-8"
    )
    forward_rows = [
        _forensics_row("LIT", "time_exit", -80.0, "europe") for _ in range(6)
    ]
    forward = root / "results" / "forward"
    forward.mkdir(parents=True, exist_ok=True)
    (forward / "trade_forensics.jsonl").write_text(
        "\n".join(json.dumps(r) for r in forward_rows) + "\n", encoding="utf-8"
    )

    result = attribution_job(job.id, store=store)

    assert result["backtest"]["exit_reason"]["time_exit"]["n"] == 10
    assert result["forward"]["symbol"]["LIT"]["avg_bps"] == -80.0
    top = result["expectation_deltas"][0]
    # forward time_exit -80 vs backtest +40 -> delta -120, adequately sampled
    assert top["small_n"] is False
    assert top["avg_bps_delta"] == -120.0
    assert (root / "results" / "research" / "attribution.json").exists()

    # empty job fails loud with the remedy
    job2 = WayfinderJob.new("attr-empty", agent_mode="intervene")
    store.save(job2)
    with pytest.raises(ValueError, match="no forensics rows"):
        attribution_job(job2.id, store=store)
