"""Trade forensics: path metrics per closed trade (MAE/MFE, post-exit
excursion, stop-survival counterfactuals) and the population aggregate."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from wayfinder_paths.jobs.execution.driver import _record_pending_trade_forensics
from wayfinder_paths.jobs.execution.primitives import CompletedBarsView
from wayfinder_paths.jobs.trade_forensics import (
    aggregate_trade_forensics,
    compute_trade_forensics,
    forensics_for_closed_trades,
    match_entry_fill,
    position_side_of_close,
)


def _ts(minute: int) -> pd.Timestamp:
    return pd.Timestamp("2026-07-22T10:00:00Z") + pd.Timedelta(minutes=5 * minute)


def _bars(prices: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    """One 5m bar per (open, high, low, close) tuple starting at minute 0."""
    return pd.DataFrame(
        [
            {
                "timestamp": _ts(i).isoformat(),
                "symbol": "LIT",
                "open": o,
                "high": h,
                "low": low,
                "close": c,
                "volume": 1.0,
            }
            for i, (o, h, low, c) in enumerate(prices)
        ]
    )


def test_short_stop_out_with_post_exit_collapse() -> None:
    # Short entered at 100 on bar0 close; price squeezes to 103 (stop) then
    # collapses to 95 in the bars after the exit — the chart pattern that
    # motivated this module.
    bars = _bars(
        [
            (100, 100.5, 99.5, 100.0),  # bar0: entry bar
            (100, 102.0, 99.9, 101.5),  # bar1: adverse
            (101.5, 103.0, 101.0, 102.8),  # bar2: stop hit intra-bar
            (102.8, 102.9, 99.0, 99.5),  # bar3: collapse begins
            (99.5, 99.6, 96.0, 96.5),  # bar4
            (96.5, 97.0, 95.0, 95.2),  # bar5
            (95.2, 95.8, 94.8, 95.5),  # bar6
        ]
    )
    row = compute_trade_forensics(
        bars,
        side="short",
        entry_ts=_ts(0),
        entry_price=100.0,
        exit_ts=_ts(2),
        exit_price=103.0,
        exit_reason="stop_loss",
        post_bars=(2, 4),
        stop_grid=(0.025, 0.035),
    )
    assert row["realized_bps"] == -300.0
    # Hold covers bars 1-2: adverse extreme high=103 -> MAE 300bps; the
    # favorable extreme low=99.9 -> MFE 1bps... low is 99.9 in bar1, 101.0 in
    # bar2 -> best favorable = 100 - 99.9 = 0.1 -> 10bps.
    assert row["hold_mae_bps"] == 300.0
    assert row["hold_mfe_bps"] == 10.0
    # Post-exit (short's favor = down, measured vs exit 103, bps of entry):
    # +2 bars close 96.5 -> (103 - 96.5) / 100 = 650bps
    assert row["post_exit_favorable_bps"]["+2"] == 650.0
    assert row["post_exit_favorable_bps"]["+4"] == 750.0
    # Best low within 4 post bars (bars 3-6) = 94.8 -> 820bps
    assert row["post_exit_best_bps"] == 820.0
    # Price fell back through the entry (100) after the stop: whipsaw signature.
    assert row["post_exit_through_entry"] is True
    assert row["stop_survives"] == {"0.025": False, "0.035": True}
    assert row["coverage"] == {"hold": True, "post_bars": 4, "post_bars_wanted": 4}


def test_long_winner_with_truncated_post_window() -> None:
    bars = _bars(
        [
            (100, 100.5, 99.5, 100.0),
            (100, 102.0, 99.8, 101.8),
            (101.8, 103.5, 101.5, 103.0),
            (103.0, 103.2, 102.0, 102.5),  # only 1 post-exit bar available
        ]
    )
    row = compute_trade_forensics(
        bars,
        side="long",
        entry_ts=_ts(0),
        entry_price=100.0,
        exit_ts=_ts(2),
        exit_price=103.0,
        exit_reason="time_exit",
        post_bars=(2, 4),
        stop_grid=(0.02,),
    )
    assert row["realized_bps"] == 300.0
    assert row["hold_mfe_bps"] == 350.0  # high 103.5
    assert row["hold_mae_bps"] == 20.0  # low 99.8
    assert row["post_exit_favorable_bps"] == {"+2": None, "+4": None}
    assert row["post_exit_best_bps"] == 20.0  # high 103.2 vs exit 103.0
    assert row["stop_survives"] == {"0.02": True}
    assert row["coverage"]["post_bars"] == 1


def test_match_entry_fill_and_close_side() -> None:
    fills = [
        {
            "symbol": "LIT",
            "side": "sell",
            "reduce_only": False,
            "timestamp": _ts(0).isoformat(),
            "avg_price": 100.0,
        },
        {
            "symbol": "XRP",
            "side": "buy",
            "reduce_only": False,
            "timestamp": _ts(1).isoformat(),
            "avg_price": 3.0,
        },
        {
            "symbol": "LIT",
            "side": "buy",
            "reduce_only": True,
            "timestamp": _ts(2).isoformat(),
            "avg_price": 103.0,
        },
    ]
    entry = match_entry_fill(fills, symbol="LIT", exit_ts=_ts(2))
    assert entry is not None and entry["avg_price"] == 100.0
    # The recorded trade row carries the CLOSING fill's side.
    assert position_side_of_close("buy") == "short"
    assert position_side_of_close("sell") == "long"


def test_forensics_for_closed_trades_end_to_end() -> None:
    bars = _bars(
        [
            (100, 100.5, 99.5, 100.0),
            (100, 102.0, 99.9, 101.5),
            (101.5, 103.0, 101.0, 103.0),
            (103.0, 103.1, 99.0, 99.5),
            (99.5, 99.6, 96.0, 96.5),
        ]
    )
    fills = [
        {
            "symbol": "LIT",
            "side": "sell",
            "reduce_only": False,
            "timestamp": _ts(0).isoformat(),
            "avg_price": 100.0,
            "raw": {"intent_metadata": {"entry_reason": "compression_break_fade"}},
        },
    ]
    trades = [
        {
            "symbol": "LIT",
            "side": "buy",
            "price": 103.0,
            "net_pnl": -0.64,
            "closed_at": _ts(2).isoformat(),
            "raw": {"intent_metadata": {"exit_reason": "stop_loss"}},
        },
    ]
    rows = forensics_for_closed_trades({"LIT": bars}, trades, fills, post_bars=(2,))
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "LIT"
    assert row["side"] == "short"
    assert row["entry_reason"] == "compression_break_fade"
    assert row["exit_reason"] == "stop_loss"
    assert row["realized_bps"] == -300.0
    assert row["net_pnl"] == -0.64


def test_aggregate_groups_by_exit_reason() -> None:
    rows = [
        {
            "exit_reason": "stop_loss",
            "realized_bps": -250.0,
            "hold_mae_bps": 260.0,
            "hold_mfe_bps": 5.0,
            "post_exit_favorable_bps": {"+4": 200.0},
            "stop_survives": {"0.035": True},
            "post_exit_through_entry": True,
        },
        {
            "exit_reason": "stop_loss",
            "realized_bps": -260.0,
            "hold_mae_bps": 270.0,
            "hold_mfe_bps": 15.0,
            "post_exit_favorable_bps": {"+4": -50.0},
            "stop_survives": {"0.035": False},
            "post_exit_through_entry": False,
        },
        {
            "exit_reason": "time_exit",
            "realized_bps": 51.0,
            "hold_mae_bps": 70.0,
            "hold_mfe_bps": 142.0,
            "post_exit_favorable_bps": {"+4": -19.0},
            "stop_survives": {"0.035": True},
            "post_exit_through_entry": False,
        },
    ]
    aggregate = aggregate_trade_forensics(rows)
    assert aggregate["trades"] == 3
    stops = aggregate["by_exit_reason"]["stop_loss"]
    assert stops["count"] == 2
    assert stops["avg_realized_bps"] == -255.0
    assert stops["avg_post_exit_favorable_bps"]["+4"] == 75.0
    assert stops["stop_survival_rate"]["0.035"] == 0.5
    assert stops["post_exit_through_entry_rate"] == 0.5
    assert aggregate["by_exit_reason"]["time_exit"]["count"] == 1


def test_driver_lazy_forensics_writes_once(tmp_path: Path) -> None:
    root = tmp_path
    forward = root / "results" / "forward"
    forward.mkdir(parents=True)
    (forward / "fills.jsonl").write_text(
        json.dumps(
            {
                "symbol": "LIT",
                "side": "sell",
                "reduce_only": False,
                "timestamp": _ts(0).isoformat(),
                "avg_price": 100.0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (forward / "trades.jsonl").write_text(
        json.dumps(
            {
                "symbol": "LIT",
                "side": "buy",
                "price": 103.0,
                "net_pnl": -0.64,
                "closed_at": _ts(2).isoformat(),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    # Window with only 3 post-exit bars: below FORENSICS_POST_BARS -> deferred.
    short_view = CompletedBarsView.from_rows(
        _bars([(100, 101, 99, 100)] * 6).to_dict("records")
    )
    assert _record_pending_trade_forensics(root, short_view) == 0
    assert not (forward / "trade_forensics.jsonl").exists()

    # Window covering exit + 16 bars -> computed exactly once.
    full_view = CompletedBarsView.from_rows(
        _bars([(100, 101, 99, 100)] * 20).to_dict("records")
    )
    assert _record_pending_trade_forensics(root, full_view) == 1
    rows = [
        json.loads(line)
        for line in (forward / "trade_forensics.jsonl").read_text().splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["symbol"] == "LIT"
    assert rows[0]["side"] == "short"

    # Idempotent: a later tick with the same window appends nothing.
    assert _record_pending_trade_forensics(root, full_view) == 0


def test_worker_forensics_block_reads_both_sources(tmp_path: Path) -> None:
    from wayfinder_paths.jobs.worker import _trade_forensics_block

    assert _trade_forensics_block(tmp_path) == {}

    forward = tmp_path / "results" / "forward"
    forward.mkdir(parents=True)
    (forward / "trade_forensics.jsonl").write_text(
        "\n".join(
            json.dumps({"symbol": "LIT", "realized_bps": bps, "coverage": {}})
            for bps in (-250.0, 51.0)
        )
        + "\n",
        encoding="utf-8",
    )
    backtest = tmp_path / "results" / "backtest"
    backtest.mkdir(parents=True)
    (backtest / "trade_forensics.json").write_text(
        json.dumps({"aggregate": {"trades": 332}, "trades": []}),
        encoding="utf-8",
    )

    block = _trade_forensics_block(tmp_path)
    assert [row["realized_bps"] for row in block["recent_forward_trades"]] == [
        -250.0,
        51.0,
    ]
    # Verbose per-trade coverage flags are stripped from the prompt payload.
    assert "coverage" not in block["recent_forward_trades"][0]
    assert block["backtest_aggregate"] == {"trades": 332}
    assert "hypothesis fuel" in block["_basis"]
