from __future__ import annotations

import json

import pytest

from wayfinder_paths.jobs.evolution_diagnostics import (
    DIAGNOSTIC_PACK_MAX_BYTES,
    attempt_made_progress,
    build_diagnostic_pack,
    build_postmortem,
    build_repair_work_order,
    compact_postmortem,
    participation_adjusted_score,
    receipt_economics,
    resolve_json_pointer,
    valid_evidence_pointers,
)


def _receipt(*, net: float, trades: list[dict], valid: bool = True) -> dict:
    return {
        "execution_valid": valid,
        "stats": {
            "net_return": net,
            "trade_count": len(trades),
            "exposure_pct": 0.2,
            "total_turnover_usd": 100.0,
            "total_fees": 1.0,
        },
        "objective": {"net_log_growth": net},
        "trades": trades,
    }


def _trade(timestamp: str, *, symbol: str = "BTC", pnl: float = 1.0) -> dict:
    return {
        "timestamp": timestamp,
        "symbol": symbol,
        "side": "buy",
        "filled_size": 1.0,
        "fee": 0.1,
        "realized_pnl_delta": pnl,
        "reduce_only": False,
        "action": "OPEN_LONG",
    }


def test_postmortem_is_a_same_window_behavior_and_cost_diff() -> None:
    reference = _receipt(net=-0.01, trades=[_trade("2026-08-01T00:00:00Z")])
    candidate = _receipt(
        net=0.02,
        trades=[
            _trade("2026-08-01T00:00:00Z"),
            _trade("2026-08-01T01:00:00Z", symbol="ETH"),
        ],
    )

    report = build_postmortem(candidate, reference, min_trades=1)

    assert report["viable"] is True
    assert report["failure_codes"] == []
    assert report["behavior_diff"]["decisions_added"] == 1
    assert report["behavior_diff"]["decisions_removed"] == 0
    assert report["behavior_diff"]["trade_count_delta"] == 1
    assert report["behavior_diff"]["net_return_delta"] == pytest.approx(0.03)
    assert report["behavior_diff"]["by_symbol"]["ETH"]["fills"] == 1


def test_noop_and_negative_attempts_are_machine_legible_and_compound() -> None:
    baseline = _receipt(net=-0.02, trades=[_trade("2026-08-01T00:00:00Z")])
    noop = build_postmortem(baseline, baseline, min_trades=1)
    assert noop["viable"] is False
    assert noop["failure_codes"][:2] == [
        "no_behavior_change",
        "negative_after_costs",
    ]

    improved = _receipt(
        net=-0.01,
        trades=[
            _trade("2026-08-01T00:00:00Z"),
            _trade("2026-08-01T01:00:00Z"),
        ],
    )
    second = build_postmortem(improved, baseline, previous=baseline, min_trades=1)
    assert second["viable"] is False
    assert attempt_made_progress(second) is True


def test_compact_postmortem_preserves_bounded_repair_cause() -> None:
    report = compact_postmortem(
        {
            "viable": False,
            "primary_failure": "invalid_execution",
            "failure_codes": ["invalid_execution"],
            "behavior_diff": {"material_change": False},
            "repair_context": {"error": "window invariant failed " + "x" * 900},
        }
    )

    assert report["repair_context"]["error"].startswith("window invariant failed")
    assert len(report["repair_context"]["error"]) == 500


def test_diagnostic_pack_is_bounded_and_citations_are_exact(tmp_path) -> None:
    research = tmp_path / "results" / "research"
    research.mkdir(parents=True)
    (research / "attribution.json").write_text(
        json.dumps(
            {
                "forward_trades": 12,
                "forward": {
                    "archetype": {f"cell-{i}": {"blob": "x" * 2_000} for i in range(30)}
                },
                "expectation_deltas": [
                    {"name": f"delta-{i}", "blob": "y" * 2_000} for i in range(20)
                ],
            }
        )
    )

    pack = build_diagnostic_pack(
        tmp_path,
        campaign_id="campaign-1",
        created_at="2026-08-31T00:00:00+00:00",
        baseline={"available": False, "reason": "no runnable fixture"},
        historical_lessons={"outcomes": []},
        research_context={"validated_positives": []},
    )

    assert len(json.dumps(pack).encode()) <= DIAGNOSTIC_PACK_MAX_BYTES
    assert resolve_json_pointer(pack, "/baseline/reason") == "no runnable fixture"
    pointers = valid_evidence_pointers(pack)
    assert "/baseline/reason" in pointers
    assert "/research_context/validated_positives" not in pointers
    with pytest.raises(ValueError, match="does not exist"):
        resolve_json_pointer(pack, "/baseline/invented")


def _churner(
    *, days: float, capital: float, fills: int, fees: float, net: float
) -> dict:
    trades = [
        _trade(f"2026-06-{18 + (index % 10):02d}T00:00:00Z", pnl=-0.05)
        for index in range(fills)
    ]
    return {
        "execution_valid": True,
        "stats": {
            "net_return": net,
            "trade_count": fills,
            "exposure_pct": 0.95,
            "total_turnover_usd": 58_706.0,
            "total_fees": fees,
            "avg_trade_duration_s": 1_800.0,
        },
        "window": {"days": days, "bars": 9_500, "starting_equity": capital},
        "objective": {"net_log_growth": net},
        "trades": trades,
    }


INCUMBENT_ECONOMICS = {
    "window_days": 100.0,
    "fills_per_day": 2.7,
    "fee_pct_of_capital": 0.112,
    "fee_pct_of_capital_30d": 0.0336,
    "turnover_multiple": 224.0,
    "exposure_pct": 0.106,
    "avg_hold_minutes": 119.0,
}


def test_cost_bleed_is_primary_when_fees_dominate_a_loss() -> None:
    # The pilot's control c04: 1,931 fills in 33 days on $100, fees $29.35.
    candidate = _churner(days=33.0, capital=100.0, fills=1_931, fees=29.35, net=-0.82)
    # A de-novo reference is an empty zero-fee scaffold; it must not hide bleed.
    reference = {"execution_valid": True, "stats": {}, "window": {}, "trades": []}

    report = build_postmortem(
        candidate,
        reference,
        min_trades=1,
        incumbent_economics=INCUMBENT_ECONOMICS,
    )

    assert report["primary_failure"] == "cost_bleed"
    assert "negative_after_costs" in report["failure_codes"]
    economics = report["economics"]["candidate"]
    assert economics["fills_per_day"] == pytest.approx(58.52, abs=0.01)
    assert economics["fee_pct_of_capital"] == pytest.approx(0.2935)
    assert report["economics"]["incumbent"]["fills_per_day"] == 2.7
    compact = compact_postmortem(report)
    assert (
        compact["economics"]["candidate"]["fills_per_day"] == economics["fills_per_day"]
    )
    assert "incumbent" in compact["economics"]

    order = build_repair_work_order(
        report,
        {"max_fills_per_day_multiple": 3.0},
        params={"fee_bps": 4.5, "slippage_bps": 3.5},
    )
    assert order["primary_failure"] == "cost_bleed"
    assert "58.5 fills/day vs incumbent 2.7" in order["diagnosis"]
    assert "fees consumed 29% of capital in 33 days" in order["diagnosis"]
    assert order["budget"] == {
        "incumbent_fills_per_day": 2.7,
        "max_fills_per_day": 8.1,
        "round_trip_cost_bps": 16.0,
    }
    assert any("minimum hold" in item for item in order["admissible_repairs"])
    assert order["forbidden"]
    banded = build_repair_work_order(report, {}, min_fills_per_day=0.4)
    assert banded["budget"]["min_fills_per_day"] == 0.4
    assert "do not drive it to zero" in banded["diagnosis"]

    dead = build_repair_work_order(
        {
            "primary_failure": "no_behavior_change",
            "failure_codes": ["no_behavior_change"],
            "repair_context": {"error": "no change", "dead_params": ["max_hold_bars"]},
        }
    )
    assert "['max_hold_bars']" in dead["diagnosis"]
    assert "dead knobs" in dead["diagnosis"]

    split = {
        "viable": False,
        "primary_failure": "screen_regime_dependent",
        "failure_codes": ["screen_regime_dependent"],
        "behavior_diff": {"material_change": True},
        "screen": {
            "confidence": 0.7,
            "code": "screen_regime_dependent",
            "slices": {
                "recent": {"net_return": 0.046, "trade_count": 44, "lcb": 0.003},
                "earlier": {"net_return": -0.037, "trade_count": 41, "lcb": -0.02},
            },
        },
    }
    order = build_repair_work_order(split)
    assert "slices disagree" in order["diagnosis"]
    assert "recent: net +4.6% on 44 trades, LCB +0.30%" in order["diagnosis"]
    assert any("declare target_regimes" in item for item in order["admissible_repairs"])
    assert compact_postmortem(split)["screen"]["slices"]["earlier"]["lcb"] == -0.02

    noisy = {**split, "primary_failure": "screen_edge_not_significant"}
    assert "not significant at 70%" in build_repair_work_order(noisy)["diagnosis"]

    heavy = build_repair_work_order(
        {
            "primary_failure": "complexity_over_budget",
            "failure_codes": ["complexity_over_budget"],
            "repair_context": {
                "error": "too many gates",
                "complexity": {"comparisons": 71, "numeric_literals": 140},
                "complexity_budget": 48,
            },
        }
    )
    assert (
        "71 comparisons" in heavy["diagnosis"] and "budget of 48" in heavy["diagnosis"]
    )
    assert any("remove gates" in item for item in heavy["admissible_repairs"])

    # No incumbent economics at all: the absolute floor still catches it.
    floor_only = build_postmortem(candidate, reference, min_trades=1)
    assert floor_only["primary_failure"] == "cost_bleed"
    # A profitable churner is not bleeding; the code never fires on winners.
    winner = _churner(days=33.0, capital=100.0, fills=1_931, fees=29.35, net=0.05)
    assert (
        "cost_bleed"
        not in build_postmortem(winner, reference, min_trades=1)["failure_codes"]
    )
    assert receipt_economics({"stats": {}, "window": {}}) is None


def test_attempt_progress_requires_material_causal_change() -> None:
    base = {"behavior_diff": {"material_change": True}, "failure_codes": []}
    assert not attempt_made_progress(
        {**base, "progress_from_previous": {"trade_count_delta": 3}}
    )
    assert attempt_made_progress(
        {**base, "progress_from_previous": {"net_return_delta": 0.01}}
    )
    assert not attempt_made_progress(
        {
            "behavior_diff": {"material_change": False},
            "progress_from_previous": {"net_return_delta": 0.01},
        }
    )
    assert attempt_made_progress(
        {
            **base,
            "primary_failure": "cost_bleed",
            "progress_from_previous": {"fills_per_day_delta": -20.0},
        }
    )


def test_participation_adjustment_does_not_reward_sparse_non_trading() -> None:
    assert participation_adjusted_score(0.1, trade_count=3, target_trades=12) == (
        pytest.approx(0.025)
    )
    assert participation_adjusted_score(-0.1, trade_count=3, target_trades=12) == (
        pytest.approx(-0.4)
    )
    assert participation_adjusted_score(0.1, trade_count=0) == float("-inf")


def _close(timestamp: str, *, reason: str, pnl: float) -> dict:
    return {
        **_trade(timestamp, pnl=pnl),
        "side": "sell",
        "reduce_only": True,
        "action": "CLOSE",
        "exit_reason": reason,
    }


def test_postmortem_records_exit_reasons_and_diagnosis_names_stops() -> None:
    candidate = _receipt(
        net=-0.01,
        trades=[
            _trade("2026-08-01T00:00:00Z"),
            _close("2026-08-01T02:00:00Z", reason="bracket_stop", pnl=-3.0),
            _trade("2026-08-01T03:00:00Z"),
            _close("2026-08-01T05:00:00Z", reason="time_exit", pnl=1.5),
            _trade("2026-08-01T06:00:00Z"),
            _close("2026-08-01T08:00:00Z", reason="time_exit", pnl=0.5),
            _trade("2026-08-02T00:00:00Z"),
            _close("2026-08-02T02:00:00Z", reason="atr_stop", pnl=-2.0),
        ],
    )
    candidate["window"] = {"days": 2.0, "starting_equity": 100.0}
    reference = _receipt(net=0.0, trades=[_trade("2026-08-01T00:00:00Z")])

    report = build_postmortem(candidate, reference, min_trades=1)

    exits = report["exits"]["candidate"]
    assert exits["closes"] == 4 and exits["stop_share"] == 0.5
    assert exits["by_reason"]["time_exit"] == {
        "count": 2,
        "net_pnl": 2.0,
        "win_rate": 1.0,
    }
    assert exits["by_reason"]["bracket_stop"]["net_pnl"] == -3.0
    assert report["exits"]["reference"] is None  # opens only: nothing closed

    compact = compact_postmortem(report)
    assert compact["exits"]["candidate"]["stop_share"] == 0.5
    assert next(iter(compact["exits"]["candidate"]["by_reason"])) == "time_exit"
    assert "reference" not in compact["exits"]

    order = build_repair_work_order(report)
    assert "exits: 50% stops of 4 closes; time_exit 2 (+2.00)" in order["diagnosis"]


def test_trade_view_labels_closes_from_intent_metadata() -> None:
    from wayfinder_paths.jobs.evolution_diagnostics import _trade_view

    def fill(reduce_only: bool, metadata: dict) -> dict:
        return {
            "timestamp": "2026-08-01T00:00:00Z",
            "symbol": "BTC",
            "side": "sell" if reduce_only else "buy",
            "filled_size": 1.0,
            "avg_price": 100.0,
            "fee": 0.1,
            "realized_pnl_delta": 0.0,
            "reduce_only": reduce_only,
            "raw": {
                "intent_action": "CLOSE" if reduce_only else "OPEN",
                "intent_metadata": metadata,
            },
        }

    assert _trade_view(fill(False, {"entry_reason": "breakout"}))["entry_reason"] == (
        "breakout"
    )
    assert "exit_reason" not in _trade_view(fill(False, {}))
    assert _trade_view(fill(True, {"exit_reason": "time_exit"}))["exit_reason"] == (
        "time_exit"
    )
    # Engine closes carry a marker, not a label.
    assert _trade_view(fill(True, {"bracket": {"kind": "stop"}}))["exit_reason"] == (
        "bracket_stop"
    )
    assert _trade_view(fill(True, {"liquidation": True}))["exit_reason"] == (
        "liquidation"
    )
    assert _trade_view(fill(True, {}))["exit_reason"] == "unlabeled"
