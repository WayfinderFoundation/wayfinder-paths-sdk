"""`load_forward_view` mirrors the backtest view for FORWARD (paper/live) data:
entry/exit markers tagged with the mode they executed in, a PnL curve from the
tick ledger, and a paper-vs-live PnL split — so the jobs UI can show what a
paper strategy actually did (the week imx-short traded invisibly is the bug
this exists to fix).
"""

from __future__ import annotations

import json
from pathlib import Path

from wayfinder_paths.jobs.forward import load_forward_snapshot
from wayfinder_paths.jobs.forward_artifacts import forward_events, load_forward_view
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore


def _seed_job(tmp_path: Path) -> JobStore:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("carry", script="strategy.py", interval_seconds=3600)
    job.execution_params["initial_capital"] = 100.0
    store.create_job(job)
    forward = store.job_dir("carry") / "results" / "forward"
    forward.mkdir(parents=True, exist_ok=True)

    def write_jsonl(name: str, rows: list[dict]) -> None:
        (forward / name).write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )

    write_jsonl(
        "fills.jsonl",
        [
            {
                "kind": "fill",
                "timestamp": "2026-07-14T05:00:00+00:00",
                "symbol": "IMX",
                "side": "sell",
                "avg_price": 0.130,
                "filled_size": 700.0,
                "reduce_only": False,
                "status": "filled",
                "mode": "paper",
                "raw": {"intent_metadata": {"entry_reason": "new_low_5"}},
            },
            {
                "kind": "fill",
                "timestamp": "2026-07-15T10:00:00+00:00",
                "symbol": "IMX",
                "side": "buy",
                "avg_price": 0.128,
                "filled_size": 700.0,
                "reduce_only": True,
                "status": "filled",
                "mode": "paper",
                "raw": {"intent_metadata": {"exit_reason": "sma50_floor"}},
            },
            {
                "kind": "fill",
                "timestamp": "2026-07-16T02:00:00+00:00",
                "symbol": "IMX",
                "side": "sell",
                "avg_price": 0.125,
                "filled_size": 700.0,
                "reduce_only": False,
                "status": "filled",
                "mode": "live",
            },
            {
                "kind": "fill",
                "timestamp": "2026-07-16T09:00:00+00:00",
                "symbol": "IMX",
                "side": "buy",
                "avg_price": 0.126,
                "filled_size": 700.0,
                "reduce_only": True,
                "status": "filled",
                "mode": "live",
            },
        ],
    )
    write_jsonl(
        "trades.jsonl",
        [
            {
                "kind": "trade",
                "symbol": "IMX",
                "side": "buy",
                "net_pnl": 1.4,
                "closed_at": "2026-07-15T10:00:00+00:00",
                "mode": "paper",
            },
            {
                "kind": "trade",
                "symbol": "IMX",
                "side": "buy",
                "net_pnl": -0.7,
                "closed_at": "2026-07-16T09:00:00+00:00",
                "mode": "live",
            },
        ],
    )
    write_jsonl(
        "ticks.jsonl",
        [
            {
                "kind": "tick",
                "bar_ts": f"2026-07-14T{hour:02d}:00:00+00:00",
                "mode": "paper",
                "ledger": {"realized_pnl": 0.0, "positions": {}},
            }
            for hour in range(4)
        ]
        + [
            {
                "kind": "tick",
                "bar_ts": "2026-07-15T10:00:00+00:00",
                "mode": "paper",
                "ledger": {"realized_pnl": 1.4, "positions": {}},
            },
            {
                "kind": "tick",
                "bar_ts": "2026-07-16T09:00:00+00:00",
                "mode": "live",
                "ledger": {
                    "realized_pnl": 0.7,
                    "positions": {
                        "IMX": {
                            "side": "short",
                            "size": 500.0,
                            "avg_price": 0.124,
                            "opened_at": "2026-07-16T12:00:00+00:00",
                        }
                    },
                },
            },
        ],
    )
    return store


def test_markers_carry_mode_and_kind(tmp_path: Path) -> None:
    store = _seed_job(tmp_path)
    result = load_forward_view("carry", store=store, include_prices=False)
    assert result["available"] is True
    markers = result["visualization"]["markers"]
    assert [(m["kind"], m["mode"]) for m in markers] == [
        ("entry", "paper"),
        ("exit", "paper"),
        ("entry", "live"),
        ("exit", "live"),
    ]
    assert markers[0]["label"] == "paper entry: new_low_5"
    assert markers[1]["label"] == "paper exit: sma50_floor"


def test_pnl_by_mode_splits_paper_and_live(tmp_path: Path) -> None:
    store = _seed_job(tmp_path)
    result = load_forward_view("carry", store=store, include_prices=False)
    summary = result["summary"]
    assert summary["pnl_by_mode"] == {"paper": 1.4, "live": -0.7}
    assert summary["trades_by_mode"] == {"paper": 1, "live": 1}


def test_pnl_curve_uses_initial_capital(tmp_path: Path) -> None:
    store = _seed_job(tmp_path)
    result = load_forward_view("carry", store=store, include_prices=False)
    equity = next(
        s for s in result["visualization"]["series"] if s["kind"] == "equity_curve"
    )
    assert equity["points"][0]["value"] == 100.0
    assert equity["points"][-1]["value"] == 100.7
    # Points carry the mode of the tick they came from.
    assert equity["points"][0]["mode"] == "paper"
    assert equity["points"][-1]["mode"] == "live"


def test_open_position_reported(tmp_path: Path) -> None:
    store = _seed_job(tmp_path)
    result = load_forward_view("carry", store=store, include_prices=False)
    position = result["summary"]["open_position"]
    assert position["symbol"] == "IMX"
    assert position["side"] == "short"
    assert position["size"] == 500.0
    assert position["mode"] == "live"


def test_downsampling_caps_points(tmp_path: Path) -> None:
    store = _seed_job(tmp_path)
    forward = store.job_dir("carry") / "results" / "forward"
    rows = [
        json.dumps(
            {
                "kind": "tick",
                "bar_ts": f"2026-07-{1 + hour // 24:02d}T{hour % 24:02d}:00:00+00:00",
                "mode": "paper",
                "ledger": {"realized_pnl": float(hour), "positions": {}},
            }
        )
        for hour in range(600)
    ]
    (forward / "ticks.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")

    result = load_forward_view(
        "carry", store=store, include_prices=False, max_points=200
    )
    equity = next(
        s for s in result["visualization"]["series"] if s["kind"] == "equity_curve"
    )
    assert len(equity["points"]) == 200
    # First and last points survive the stride.
    assert equity["points"][0]["realized_pnl"] == 0.0
    assert equity["points"][-1]["realized_pnl"] == 599.0


def test_price_fetch_failure_degrades_with_note(tmp_path: Path) -> None:
    store = _seed_job(tmp_path)
    # No execution_spec exists in the seeded job -> the price fetch raises and
    # the view degrades to markers + PnL with a note instead of failing.
    result = load_forward_view("carry", store=store, include_prices=True)
    assert result["available"] is True
    assert "price_note" in result["summary"]
    kinds = {s["kind"] for s in result["visualization"]["series"]}
    assert kinds == {"equity_curve"}
    assert len(result["visualization"]["markers"]) == 4


def test_view_filter_selects_kinds(tmp_path: Path) -> None:
    store = _seed_job(tmp_path)
    result = load_forward_view(
        "carry", store=store, include_prices=False, view="equity"
    )
    kinds = {s["kind"] for s in result["visualization"]["series"]}
    assert kinds == {"equity_curve"}


def test_unavailable_when_no_forward_data(tmp_path: Path) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("empty", script="strategy.py", interval_seconds=3600)
    store.create_job(job)
    assert load_forward_view("empty", store=store) == {"available": False}


def test_snapshot_summary_includes_split_and_position(tmp_path: Path) -> None:
    store = _seed_job(tmp_path)
    snapshot = load_forward_snapshot("carry", store=store)
    summary = snapshot["summary"]
    assert summary["pnl_by_mode"] == {"paper": 1.4, "live": -0.7}
    assert summary["trades_by_mode"] == {"paper": 1, "live": 1}
    assert summary["open_position"]["symbol"] == "IMX"


def test_forward_events_annotate_lifecycle(tmp_path: Path) -> None:
    """Mode flips, revision changes (labeled from the matching proposal), and
    halt engagements become chart events; steady-state ticks emit nothing."""
    ticks = [
        {"bar_ts": "2026-07-14T00:00:00+00:00", "mode": "paper", "revision": "aaa111"},
        {"bar_ts": "2026-07-14T01:00:00+00:00", "mode": "paper", "revision": "aaa111"},
        {"bar_ts": "2026-07-14T02:00:00+00:00", "mode": "live", "revision": "aaa111"},
        {"bar_ts": "2026-07-14T03:00:00+00:00", "mode": "live", "revision": "bbb222"},
        {
            "bar_ts": "2026-07-14T04:00:00+00:00",
            "mode": "live",
            "revision": "bbb222",
            "guard_events": [{"kind": "manual_halt", "reason": "fat finger"}],
        },
        {  # halt persists -> no second event
            "bar_ts": "2026-07-14T05:00:00+00:00",
            "mode": "live",
            "revision": "bbb222",
            "guard_events": [{"kind": "manual_halt", "reason": "fat finger"}],
        },
    ]
    proposals = [
        {
            "summary": "Widen the stop to 9%",
            "candidate_report": {"revision": "bbb222"},
        }
    ]
    events = forward_events(ticks, proposals=proposals)
    assert [(e["kind"], e["timestamp"][11:13]) for e in events] == [
        ("mode_flip", "02"),
        ("revision", "03"),
        ("halt", "04"),
    ]
    assert events[0]["label"] == "\u2192 LIVE"
    assert events[1]["label"] == "Widen the stop to 9%"
    assert events[2]["label"] == "fat finger"


def test_forward_view_includes_events(tmp_path: Path) -> None:
    store = _seed_job(tmp_path)
    forward = store.job_dir("carry") / "results" / "forward"
    rows = [
        {
            "kind": "tick",
            "bar_ts": "2026-07-14T00:00:00+00:00",
            "mode": "paper",
            "revision": "aaa111",
            "ledger": {"realized_pnl": 0.0, "positions": {}},
        },
        {
            "kind": "tick",
            "bar_ts": "2026-07-14T01:00:00+00:00",
            "mode": "live",
            "revision": "aaa111",
            "ledger": {"realized_pnl": 0.0, "positions": {}},
        },
    ]
    (forward / "ticks.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    result = load_forward_view("carry", store=store, include_prices=False)
    events = result["visualization"]["events"]
    assert [(e["kind"], e["mode"]) for e in events] == [("mode_flip", "live")]
