"""`load_forward_view` mirrors the backtest view for FORWARD (paper/live) data:
entry/exit markers tagged with the mode they executed in, a PnL curve from the
tick ledger, and a paper-vs-live PnL split — so the jobs UI can show what a
paper strategy actually did (the week imx-short traded invisibly is the bug
this exists to fix).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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
    assert markers[0]["label"] == "paper short entry: new_low_5"
    assert markers[1]["label"] == "paper short exit: sma50_floor"
    assert markers[0]["direction"] == "short"


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


def test_open_position_survives_skipped_tick(tmp_path: Path) -> None:
    """Skipped ticks (no_new_bar) record an empty top-level ledger with the
    unchanged state under engine_state_pre — the open position must not vanish
    from the snapshot between bars."""
    store = _seed_job(tmp_path)
    forward = store.job_dir("carry") / "results" / "forward"
    skipped = {
        "kind": "tick",
        "bar_ts": "2026-07-16T09:00:00+00:00",
        "mode": "live",
        "skipped": True,
        "skip_reason": "no_new_bar",
        "ledger": {},
        "engine_state_pre": {
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
            }
        },
    }
    with (forward / "ticks.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(skipped) + "\n")

    result = load_forward_view("carry", store=store, include_prices=False)
    position = result["summary"]["open_position"]
    assert position["symbol"] == "IMX"
    assert position["side"] == "short"
    assert position["size"] == 500.0


def test_series_order_puts_marked_symbol_first(tmp_path: Path) -> None:
    """With a multi-symbol data contract only the traded symbol carries
    markers; it must sort ahead of the equity curve and untraded siblings so
    UIs that render the first couple of series show the markers."""
    from wayfinder_paths.jobs.backtest_artifacts import order_series_for_display

    series = [
        {"name": "equity", "kind": "equity_curve", "symbol": None},
        {"name": "CL_price", "kind": "market_price", "symbol": "xyz:CL"},
        {"name": "MU_price", "kind": "market_price", "symbol": "xyz:MU"},
        {"name": "SNDK_price", "kind": "market_price", "symbol": "xyz:SNDK"},
    ]
    markers = [{"symbol": "xyz:SNDK", "kind": "entry"}]
    ordered = order_series_for_display(series, markers)
    assert [entry["name"] for entry in ordered] == [
        "SNDK_price",
        "equity",
        "CL_price",
        "MU_price",
    ]


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


def test_price_series_tagged_with_venue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Market-price series carry the venue whose feed produced their bars so
    the UI can swap the static payload for that venue's live chart."""
    import wayfinder_paths.jobs.execution.venues as venues_module
    from wayfinder_paths.jobs.execution.primitives import CompletedBarsView

    store = _seed_job(tmp_path)
    spec_path = store.job_dir("carry") / "execution_spec.json"
    spec_path.write_text(
        json.dumps(
            {
                "data_contract": {
                    "bar_interval": "1h",
                    "symbols": ["IMX"],
                    "market_kind": "swap",
                }
            }
        ),
        encoding="utf-8",
    )

    class _Feed:
        async def get_completed_bars(
            self, symbols: list[str], interval: str, **_: object
        ) -> CompletedBarsView:
            return CompletedBarsView.from_rows(
                [
                    {
                        "timestamp": "2026-07-14T00:00:00+00:00",
                        "symbol": symbol,
                        "open": 0.13,
                        "high": 0.14,
                        "low": 0.12,
                        "close": 0.13,
                    }
                    for symbol in symbols
                ]
            )

    class _Adapter:
        feed = _Feed()

    monkeypatch.setattr(
        venues_module, "build_adapter", lambda *args, **kwargs: _Adapter()
    )

    result = load_forward_view("carry", store=store, include_prices=True)
    price = next(
        s for s in result["visualization"]["series"] if s["kind"] == "market_price"
    )
    assert price["symbol"] == "IMX"
    assert price["venue"] == "hyperliquid"


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


def _seed_freestyle_job(tmp_path: Path) -> JobStore:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "hormuz",
        script="workspace/src/hormuz.py",
        interval_seconds=300,
        execution_contract="freestyle_v1",
        source={"kind": "freestyle", "origin": "inline"},
    )
    job.execution_params["initial_capital"] = 1000.0
    store.create_job(job)
    forward = store.job_dir("hormuz") / "results" / "forward"
    forward.mkdir(parents=True, exist_ok=True)
    stamps = [
        "2026-09-15T10:00:00+00:00",
        "2026-09-15T10:05:00+00:00",
        "2026-09-15T10:10:00+00:00",
    ]
    odds = [0.62, 0.7, 0.2]
    btc = [40_000.0, 40_100.0, 39_900.0]
    equity = [1000.0, 1005.0, 1010.0]
    realized = [0.0, 0.0, 10.0]
    ticks = [
        {
            "kind": "tick",
            "ts": ts,
            "bar_ts": ts,
            "mode": "paper",
            "revision": "abc",
            "marks": {
                "polymarket:polymarket:hormuz-closure-2026:YES": odd,
                "hyperliquid:BTC": price,
            },
            "funding": {"hyperliquid:BTC": 0.0001},
            "token_values": {"ethereum-base": 2500.0},
            "yields": {"lend_supply_apr:aave-v3-base:USDC": 0.05},
            "equity": eq,
            "unrealized_pnl": eq - 1000.0 - rp,
            "ledger": {"realized_pnl": rp, "positions": {}},
            "guard_events": [],
        }
        for ts, odd, price, eq, rp in zip(
            stamps, odds, btc, equity, realized, strict=True
        )
    ]
    fills = [
        {
            "status": "filled",
            "venue": "hyperliquid",
            "symbol": "BTC",
            "side": "buy",
            "filled_size": 0.005,
            "avg_price": 40_000.0,
            "reduce_only": False,
            "timestamp": stamps[0],
            "ts": stamps[0],
            "mode": "paper",
            "raw": {},
            "intent_action": "OPEN",
            "intent_metadata": {"entry_reason": "odds above 0.6"},
        },
        {
            "status": "filled",
            "venue": "hyperliquid",
            "symbol": "BTC",
            "side": "sell",
            "filled_size": 0.005,
            "avg_price": 39_900.0,
            "reduce_only": True,
            "timestamp": stamps[2],
            "ts": stamps[2],
            "mode": "paper",
            "raw": {},
            "intent_action": "CLOSE",
            "intent_metadata": {"exit_reason": "odds below 0.4"},
        },
    ]
    trades = [
        {
            "ts": stamps[2],
            "symbol": "BTC",
            "venue": "hyperliquid",
            "side": "sell",
            "size": 0.005,
            "avg_price": 39_900.0,
            "fee": 0.1,
            "net_pnl": 10.0,
            "pnl": 10.0,
            "exit_reason": "odds below 0.4",
            "mode": "paper",
        }
    ]
    for name, rows in (
        ("ticks.jsonl", ticks),
        ("fills.jsonl", fills),
        ("trades.jsonl", trades),
    ):
        (forward / name).write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
    return store


def test_freestyle_marks_and_reads_become_series_without_a_spec(
    tmp_path: Path, monkeypatch
) -> None:
    from wayfinder_paths.jobs import forward_artifacts
    from wayfinder_paths.jobs.execution import validation as validation_mod

    store = _seed_freestyle_job(tmp_path)

    def _no_spec(*args, **kwargs):
        raise AssertionError("a freestyle view must not resolve an execution spec")

    monkeypatch.setattr(validation_mod, "resolve_execution_spec", _no_spec)
    monkeypatch.setattr(
        forward_artifacts,
        "_fetch_hyperliquid_bars",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("feed down")),
    )
    view = load_forward_view("hormuz", store=store)
    assert view["available"] and "price_note" not in view["summary"]
    by_name = {s["name"]: s for s in view["visualization"]["series"]}
    btc = by_name["BTC_price"]
    assert btc["kind"] == "market_price" and btc["venue"] == "hyperliquid"
    assert [p["value"] for p in btc["points"]] == [40_000.0, 40_100.0, 39_900.0]
    odds = by_name["polymarket:hormuz-closure-2026:YES_price"]
    assert odds["venue"] == "polymarket" and odds["points"][-1]["value"] == 0.2
    assert by_name["BTC_funding"]["kind"] == "funding_rate"
    assert by_name["BTC_funding"]["venue"] == "hyperliquid"
    assert by_name["token:ethereum-base"]["kind"] == "token_value"
    assert by_name["yield:lend_supply_apr:aave-v3-base:USDC"]["kind"] == "yield_rate"
    equity = by_name["forward_equity"]
    assert [p["value"] for p in equity["points"]] == [1000.0, 1005.0, 1010.0]
    assert equity["points"][1]["unrealized_pnl"] == 5.0


def test_freestyle_hyperliquid_bars_override_marks_and_views_filter(
    tmp_path: Path, monkeypatch
) -> None:
    from wayfinder_paths.jobs import forward_artifacts

    store = _seed_freestyle_job(tmp_path)
    bars = {
        "BTC": [
            {
                "timestamp": "2026-09-15T10:05:00+00:00",
                "value": 40_050.0,
                "open": 40_000.0,
                "high": 40_100.0,
                "low": 39_950.0,
                "close": 40_050.0,
                "volume": None,
            }
        ]
    }
    monkeypatch.setattr(
        forward_artifacts, "_fetch_hyperliquid_bars", lambda *a, **k: bars
    )
    legs = load_forward_view("hormuz", store=store, view="legs")
    kinds = {s["kind"] for s in legs["visualization"]["series"]}
    assert kinds == {"market_price"}
    btc = next(s for s in legs["visualization"]["series"] if s["name"] == "BTC_price")
    assert btc["points"] == bars["BTC"]
    reads = load_forward_view("hormuz", store=store, view="reads")
    kinds = {s["kind"] for s in reads["visualization"]["series"]}
    assert kinds == {"funding_rate", "token_value", "yield_rate"}


def test_freestyle_markers_and_trades_carry_reasons_and_venue(
    tmp_path: Path, monkeypatch
) -> None:
    from wayfinder_paths.jobs import forward_artifacts

    store = _seed_freestyle_job(tmp_path)
    monkeypatch.setattr(
        forward_artifacts, "_fetch_hyperliquid_bars", lambda *a, **k: {}
    )
    view = load_forward_view("hormuz", store=store)
    markers = view["visualization"]["markers"]
    assert markers[0]["kind"] == "entry" and "odds above 0.6" in markers[0]["label"]
    assert markers[0]["venue"] == "hyperliquid"
    assert markers[1]["kind"] == "exit" and "odds below 0.4" in markers[1]["label"]
    trade = view["trades"][0]
    assert trade["entry_reason"] == "odds above 0.6"
    assert trade["exit_reason"] == "odds below 0.4"
    assert trade["venue"] == "hyperliquid" and trade["entry_price"] == 40_000.0
    assert trade["duration_minutes"] == 10 and trade["net_pnl"] == 10.0


def test_declared_feature_feeds_chart_as_feature_series(tmp_path: Path) -> None:
    from wayfinder_paths.tests.test_jobs_features import _feature_job

    store, job, root = _feature_job(tmp_path)
    forward = root / "results" / "forward"
    forward.mkdir(parents=True, exist_ok=True)
    (forward / "ticks.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    "kind": "tick",
                    "bar_ts": f"2026-01-01T00:{minute:02d}:00+00:00",
                    "mode": "paper",
                    "ledger": {"realized_pnl": 0.0, "positions": {}},
                }
            )
            + "\n"
            for minute in (0, 5, 10, 15)
        ),
        encoding="utf-8",
    )
    view = load_forward_view(job.id, store=store, view="reads", include_prices=False)
    series = view["visualization"]["series"]
    assert [s["kind"] for s in series] == ["feature"]
    assert series[0]["name"] == "feature:sentiment" and series[0]["key"] == "sentiment"
    assert [p["value"] for p in series[0]["points"]] == [0.9, -0.9]
    everything = load_forward_view(job.id, store=store, include_prices=False)
    assert {s["kind"] for s in everything["visualization"]["series"]} == {
        "equity_curve",
        "feature",
    }
