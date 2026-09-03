"""Trade forensics: path metrics per closed trade (MAE/MFE, post-exit
excursion, stop-survival counterfactuals) and the population aggregate."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from wayfinder_paths.jobs.execution.driver import (
    _record_pending_trade_forensics,
    _trade_close_payload,
)
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
        exit_reason=None,  # bracket fills carry no strategy label
        post_bars=(2, 4),
        stop_grid=(0.025, 0.035),
    )
    assert row["exit_reason"] == "bracket_stop"
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
    assert row["exit_reason"] == "time_exit"
    assert row["realized_bps"] == 300.0
    assert row["hold_mfe_bps"] == 350.0  # high 103.5
    assert row["hold_mae_bps"] == 20.0  # low 99.8
    assert row["post_exit_favorable_bps"] == {"+2": None, "+4": None}
    assert row["post_exit_best_bps"] == 20.0  # high 103.2 vs exit 103.0
    assert row["stop_survives"] == {"0.02": True}
    assert row["coverage"]["post_bars"] == 1


def test_bracket_stop_survival_scans_post_window() -> None:
    # Short stopped at 103; the post window squeezes HIGHER to 104.5 before
    # reversing. A 3.5% stop survives the truncated hold (MAE 300bps) but NOT
    # the continued hypothetical hold (450bps) — the extended scan must catch
    # that, or the counterfactual overstates survival.
    bars = _bars(
        [
            (100, 100.5, 99.5, 100.0),
            (100, 103.0, 99.9, 102.8),  # stop hit
            (102.8, 104.5, 102.5, 104.0),  # post: squeeze continues
            (104.0, 104.2, 101.0, 101.5),
            (101.5, 101.6, 99.0, 99.5),
        ]
    )
    row = compute_trade_forensics(
        bars,
        side="short",
        entry_ts=_ts(0),
        entry_price=100.0,
        exit_ts=_ts(1),
        exit_price=103.0,
        exit_reason=None,
        post_bars=(2,),
        stop_grid=(0.035, 0.05),
    )
    assert row["exit_reason"] == "bracket_stop"
    assert row["hold_mae_bps"] == 300.0  # truncated-hold MAE alone
    assert row["stop_survives"] == {"0.035": False, "0.05": True}

    # The same path with a LABELED exit scans only the actual hold: the
    # position would not exist post-exit under any stop width.
    labeled = compute_trade_forensics(
        bars,
        side="short",
        entry_ts=_ts(0),
        entry_price=100.0,
        exit_ts=_ts(1),
        exit_price=103.0,
        exit_reason="time_exit",
        post_bars=(2,),
        stop_grid=(0.035, 0.05),
    )
    assert labeled["stop_survives"] == {"0.035": True, "0.05": True}


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


def test_exit_reason_falls_back_to_closing_fill() -> None:
    # Forward trade-close rows carry no intent metadata: the reason lives on
    # the closing fill. An unlabeled closing fill means the bracket fired.
    bars = _bars(
        [
            (100, 100.5, 99.5, 100.0),
            (100, 101, 99, 100.5),
            (100.5, 101, 99, 100.0),
            (100, 101, 99, 100.2),
        ]
    )
    fills = [
        {
            "symbol": "LIT",
            "side": "sell",
            "reduce_only": False,
            "timestamp": _ts(0).isoformat(),
            "avg_price": 100.0,
        },
        {
            "symbol": "LIT",
            "side": "buy",
            "reduce_only": True,
            "timestamp": _ts(2).isoformat(),
            "avg_price": 100.0,
            "raw": {"intent_metadata": {"exit_reason": "time_exit"}},
        },
    ]
    trades = [
        {
            "symbol": "LIT",
            "side": "buy",
            "price": 100.0,
            "closed_at": _ts(2).isoformat(),
        },
    ]
    rows = forensics_for_closed_trades({"LIT": bars}, trades, fills, post_bars=(1,))
    assert rows[0]["exit_reason"] == "time_exit"

    # Same trade but the closing fill has no label -> bracket_stop.
    fills[1]["raw"] = {"intent_metadata": {"exit_reason": ""}}
    rows = forensics_for_closed_trades({"LIT": bars}, trades, fills, post_bars=(1,))
    assert rows[0]["exit_reason"] == "bracket_stop"


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


def test_stop_close_payload_preserves_trigger_and_slippage_evidence() -> None:
    payload = _trade_close_payload(
        {
            "venue": "hyperliquid",
            "symbol": "HYPE",
            "side": "buy",
            "filled_size": 2.0,
            "avg_price": 110.0,
            "fee": 0.25,
            "reduce_only": True,
            "realized_pnl_delta": -10.0,
            "timestamp": _ts(3).isoformat(),
            "raw": {
                "intent_action": "STOP_LOSS",
                "intent_metadata": {
                    "bracket": {
                        "trigger_price": 100.0,
                        "price": 105.0,
                        "gap_at_open": True,
                    }
                },
            },
        },
        params={"leverage": 3},
    )

    assert payload["exit_reason"] == "bracket_stop"
    assert payload["stop_trigger_price"] == 100.0
    assert payload["stop_slippage_bps"] == 1_000.0
    assert payload["effective_leverage"] == 3
    assert payload["venue_stop_slippage_tolerance_bps"] == 1_000


def test_fill_exit_reason_is_one_rule_for_every_consumer() -> None:
    from wayfinder_paths.jobs.trade_forensics import (
        fill_exit_reason,
        is_stop_exit_reason,
    )

    assert fill_exit_reason({"exit_reason": "tp_tier_one"}) == "tp_tier_one"
    assert fill_exit_reason({"bracket": {"kind": "stop"}}) == "bracket_stop"
    assert fill_exit_reason({"liquidation": True, "position_side": "long"}) == (
        "liquidation"
    )
    assert fill_exit_reason({"stale_policy": "flat"}) == "stale_flat"
    assert fill_exit_reason({}) == "unlabeled"
    assert fill_exit_reason(None) == "unlabeled"
    assert is_stop_exit_reason("bracket_stop") and is_stop_exit_reason("atr_stop")
    assert not is_stop_exit_reason("time_exit") and not is_stop_exit_reason(None)
