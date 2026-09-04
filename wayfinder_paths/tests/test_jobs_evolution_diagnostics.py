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
    # The open route is composition on the macro column; the specialist bar
    # is offered only where the campaign accepts target_regimes.
    assert any("macro_regime" in item for item in order["admissible_repairs"])
    assert not any("target_regimes" in item for item in order["admissible_repairs"])
    assert any(
        "declare target_regimes" in item
        for item in build_repair_work_order(split, {"regime_specialist_enabled": True})[
            "admissible_repairs"
        ]
    )
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
    # Screened attempts are judged on the screen's lower bound alone.
    assert not attempt_made_progress(
        {
            **base,
            "progress_from_previous": {
                "net_return_delta": 0.01,
                "screen_lcb_delta": 0.001,
            },
        }
    )
    assert attempt_made_progress(
        {**base, "progress_from_previous": {"screen_lcb_delta": 0.02}}
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


def test_pack_budget_trims_lessons_before_dropping_them() -> None:
    from wayfinder_paths.jobs.evolution_diagnostics import _fit_pack

    outcomes = [
        {
            "candidate_id": f"c{i:02d}",
            "family": f"family-{i}",
            "status": "low_fidelity_rejected",
            "quick_result": {"net_return": 0.01, "trade_count": 20},
            "validation_exits": {"closes": 8, "stop_share": 0.25},
            "postmortem": {"failure_codes": ["x"], "blob": "p" * 900},
            "validation_forensics": {"time_exit": {"count": 8, "blob": "f" * 400}},
        }
        for i in range(16)
    ]
    pack = {
        "schema_version": "1.0",
        "baseline": {"stats": {"net_return": 0.0}},
        "validated_signals": {"blob": "s" * 9_000},
        "research_ideation": {"blob": "i" * 4_500},
        "research_context": {"refuted_families": [{"family": "f"}]},
        "prior_campaign_lessons": {"outcomes": outcomes, "_basis": "prior outcomes"},
    }

    fitted = _fit_pack(pack)

    assert len(json.dumps(fitted).encode()) <= DIAGNOSTIC_PACK_MAX_BYTES
    kept = fitted["prior_campaign_lessons"]["outcomes"]
    assert len(kept) == 12
    assert kept[0]["validation_exits"] == {"closes": 8, "stop_share": 0.25}
    assert "postmortem" not in kept[0] and "validation_forensics" not in kept[0]
    assert "prior_campaign_lessons_truncated" not in fitted


def test_screen_slices_carry_their_macro_regime_into_the_diagnosis() -> None:
    postmortem = {
        "primary_failure": "screen_regime_dependent",
        "failure_codes": ["screen_regime_dependent"],
        "screen": {
            "confidence": 0.7,
            "code": "screen_regime_dependent",
            "slices": {
                "recent": {
                    "net_return": 0.031,
                    "trade_count": 29,
                    "max_drawdown_pct": 0.04,
                    "macro_regime": "chop",
                    "lcb": -0.01,
                    "route": None,
                },
                "earlier": {
                    "net_return": -0.062,
                    "trade_count": 22,
                    "max_drawdown_pct": 0.12,
                    "macro_regime": "bull",
                    "lcb": -0.05,
                    "route": None,
                },
            },
        },
    }

    compact = compact_postmortem(postmortem)
    assert compact["screen"]["slices"]["earlier"]["macro_regime"] == "bull"
    diagnosis = build_repair_work_order(postmortem)["diagnosis"]
    assert "earlier (bull): net -6.2% on 22 trades" in diagnosis
    assert "recent (chop): net +3.1%" in diagnosis


def test_pack_exposes_the_macro_regime_when_the_specialist_cells_are_off(
    tmp_path,
) -> None:
    pack = build_diagnostic_pack(
        tmp_path,
        campaign_id="c1",
        created_at="2026-08-24T15:25:00+00:00",
        baseline={"stats": {"net_return": 0.0}},
        historical_lessons={},
        research_context={},
        regime_context={
            "enabled": False,
            "available": False,
            "macro": {
                "recent": {"7d": {"label": "bull", "median_return": 0.41}},
                "coverage": {"has_bull_leg": False},
            },
        },
    )

    assert "campaign_regime" not in pack
    assert pack["macro_regime"]["recent"]["7d"]["label"] == "bull"


def test_no_trades_diagnosis_names_the_frozen_state_key_and_the_primitive() -> None:
    from wayfinder_paths.jobs.evolution_diagnostics import REPAIR_REMEDIES

    preview = {
        "status": "armed_no_entry",
        "bars_replayed": 2000,
        "intents_total": 0,
        "entries": 0,
        "by_action": {},
        "state_keys": {
            "fr_arm:HYPE": {
                "first_set_bar": "b1",
                "last_changed_bar": "b1",
                "changes": 1,
            }
        },
        "frozen_after": "2026-08-01T00:00:00+00:00",
    }
    postmortem = {
        "viable": False,
        "primary_failure": "activity_below_floor",
        "failure_codes": ["activity_below_floor", "no_trades", "negative_after_costs"],
        "behavior_diff": {"material_change": True},
        "repair_context": {"sequence_preview": preview},
    }

    order = build_repair_work_order(postmortem)
    assert order["admissible_repairs"] == REPAIR_REMEDIES["no_trades"]["admissible"]
    assert "fr_arm:HYPE" in order["diagnosis"]
    assert "ctx.bar_ordinal" in order["diagnosis"]
    assert "froze after 2026-08-01" in order["diagnosis"]
    compact = compact_postmortem(postmortem)
    assert compact["repair_context"]["sequence_preview"]["status"] == "armed_no_entry"

    silent = {
        **postmortem,
        "repair_context": {
            "sequence_preview": {**preview, "status": "silent", "state_keys": {}}
        },
    }
    assert "never admits an entry" in build_repair_work_order(silent)["diagnosis"]
    stale = {
        **postmortem,
        "primary_failure": "no_progress_preview",
        "failure_codes": ["no_progress_preview", "no_trades"],
        "repair_context": {"sequence_preview": preview, "previous_preview": preview},
    }
    assert (
        "changed nothing the replay can see"
        in build_repair_work_order(stale)["diagnosis"]
    )


def test_preview_progress_requires_new_intent_or_transition() -> None:
    from wayfinder_paths.jobs.evolution_diagnostics import preview_progress

    base = {
        "status": "armed_no_entry",
        "intents_total": 0,
        "entries": 0,
        "state_keys": {"arm": {"changes": 1}},
    }
    assert preview_progress(base, base) is False
    assert preview_progress(base, {**base, "intents_total": 1}) is True
    assert (
        preview_progress(base, {**base, "state_keys": {"arm": {"changes": 2}}}) is True
    )
    assert (
        preview_progress(
            base,
            {**base, "state_keys": {"arm": {"changes": 1}, "cooldown": {"changes": 1}}},
        )
        is True
    )
    assert preview_progress(None, base) is True
    assert preview_progress(base, {"status": "skipped"}) is True
    # A no-trade repair whose replay moved keeps its budget.
    assert (
        attempt_made_progress({"progress_from_previous": {"preview_progress": True}})
        is True
    )


def test_compact_postmortem_keeps_leader_attribution_and_the_diagnosis_names_it() -> (
    None
):
    from wayfinder_paths.jobs.evolution_diagnostics import leader_attribution_sentence

    attribution = {
        "days": 40,
        "labelled_days": 40,
        "rally": {
            "days": 2,
            "day_share": 0.05,
            "loss_share": 0.28,
            "net_log_growth": -0.03,
        },
        "selloff": {
            "days": 12,
            "day_share": 0.30,
            "loss_share": 0.10,
            "net_log_growth": -0.01,
        },
    }
    assert leader_attribution_sentence(attribution) == (
        "28% of losses on broad-rally days (5% of days)"
    )
    assert (
        leader_attribution_sentence({"rally": {"day_share": 0.4, "loss_share": 0.4}})
        == ""
    )
    assert leader_attribution_sentence(None) == ""

    postmortem = {
        "primary_failure": "screen_edge_not_significant",
        "failure_codes": ["screen_edge_not_significant"],
        "screen": {
            "confidence": 0.7,
            "code": "screen_edge_not_significant",
            "slices": {
                "recent": {
                    "net_return": 0.02,
                    "trade_count": 30,
                    "macro_regime": "chop",
                    "leader_attribution": attribution,
                    "lcb": -0.01,
                    "route": None,
                }
            },
        },
    }
    compact = compact_postmortem(postmortem)
    assert (
        compact["screen"]["slices"]["recent"]["leader_attribution"]["rally"][
            "loss_share"
        ]
        == 0.28
    )
    assert (
        "28% of losses on broad-rally days (5% of days)"
        in build_repair_work_order(postmortem)["diagnosis"]
    )

    negative = _receipt(net=-0.01, trades=[_trade("2026-08-01T00:00:00Z", pnl=-1.0)])
    negative["window"] = {"days": 2.0, "starting_equity": 100.0}
    report = build_postmortem(negative, _receipt(net=0.0, trades=[]), min_trades=1)
    report["screen"] = postmortem["screen"]
    diagnosis = build_repair_work_order(report)["diagnosis"]
    assert "28% of losses on broad-rally days (5% of days)." in diagnosis


def test_trade_view_labels_take_profit_and_stop_intents_by_action() -> None:
    from wayfinder_paths.jobs.evolution_diagnostics import _trade_view

    def fill(action: str, metadata: dict) -> dict:
        return {
            "timestamp": "2026-08-01T00:00:00Z",
            "symbol": "BTC",
            "side": "sell",
            "filled_size": 1.0,
            "avg_price": 100.0,
            "fee": 0.1,
            "realized_pnl_delta": 0.0,
            "reduce_only": True,
            "raw": {"intent_action": action, "intent_metadata": metadata},
        }

    # The intent kind labels an unlabeled take-profit or stop; a stage rides along.
    assert _trade_view(fill("TAKE_PROFIT", {}))["exit_reason"] == "take_profit"
    assert _trade_view(fill("TAKE_PROFIT", {"exit_stage": "tp1"}))["exit_reason"] == (
        "take_profit:tp1"
    )
    assert _trade_view(fill("STOP_LOSS", {}))["exit_reason"] == "stop_loss"
    # An explicit label still wins; a plain CLOSE is still unlabeled.
    assert _trade_view(fill("TAKE_PROFIT", {"exit_reason": "tp_hit"}))[
        "exit_reason"
    ] == ("tp_hit")
    assert _trade_view(fill("CLOSE", {}))["exit_reason"] == "unlabeled"


def test_regime_dependent_work_order_names_the_composition_route() -> None:
    from wayfinder_paths.jobs.evolution_diagnostics import build_repair_work_order

    postmortem = {
        "primary_failure": "screen_regime_dependent",
        "failure_codes": ["screen_regime_dependent"],
        "screen": {
            "slices": {
                "earlier": {
                    "macro_regime": "chop",
                    "net_return": -0.11,
                    "trade_count": 88,
                },
                "recent": {
                    "macro_regime": "bear",
                    "net_return": 0.096,
                    "trade_count": 104,
                },
            },
            "confidence": 0.7,
        },
    }
    order = build_repair_work_order(postmortem)
    joined = " ".join(order["admissible_repairs"])
    assert "macro_regime" in joined and "other slice" in joined
    assert "target_regimes" not in joined  # the specialist path is off by default
    assert "macro_regime" in order["diagnosis"]
    enabled = build_repair_work_order(postmortem, {"regime_specialist_enabled": True})
    assert any("target_regimes" in r for r in enabled["admissible_repairs"])


def test_slice_loss_bound_work_order_says_stand_aside_or_size_down() -> None:
    from wayfinder_paths.jobs.evolution_diagnostics import (
        build_repair_work_order,
        compact_postmortem,
    )

    postmortem = {
        "primary_failure": "screen_slice_loss_bound",
        "failure_codes": ["screen_slice_loss_bound"],
        "screen": {
            "confidence": 0.7,
            "combined_net_return": -0.12,
            "pooled_lcb": -0.2,
            "max_slice_loss": 0.02,
            "overdrawn": ["earlier"],
            "slices": {
                "earlier": {
                    "macro_regime": "chop",
                    "net_return": -0.253,
                    "trade_count": 128,
                },
                "recent": {
                    "macro_regime": "bear",
                    "net_return": 0.149,
                    "trade_count": 114,
                },
            },
        },
    }
    order = build_repair_work_order(postmortem)
    assert "more than 2% in earlier" in order["diagnosis"]
    assert any("stand aside" in item for item in order["admissible_repairs"])
    compact = compact_postmortem(postmortem)["screen"]
    assert compact["overdrawn"] == ["earlier"] and compact["max_slice_loss"] == 0.02


def test_receipt_economics_reports_per_trade_cost_coverage() -> None:
    from wayfinder_paths.jobs.evolution_diagnostics import (
        REPAIR_REMEDIES,
        build_repair_work_order,
        receipt_economics,
    )

    receipt = {
        "window": {"days": 341.0, "starting_equity": 100.0},
        "round_trip_cost_bps": 17.0,
        "stats": {
            "trade_count": 887,
            "total_fees": 20.56,
            "net_return": -0.4411,
            "total_turnover_usd": 41115.6,
            "exposure_pct": 0.099,
            "avg_trade_duration_s": 7003.0,
        },
    }
    economics = receipt_economics(receipt)
    # gross = net PnL + fees = -44.11 + 20.56 = -23.55 on 20,557.8 of notional.
    assert economics["gross_bps_per_trade"] == pytest.approx(-11.46, abs=0.05)
    assert economics["round_trip_cost_bps"] == 17.0
    assert economics["cost_coverage"] == pytest.approx(-0.674, abs=0.005)
    assert "cost_not_covered" in REPAIR_REMEDIES
    order = build_repair_work_order(
        {
            "primary_failure": "cost_not_covered",
            "failure_codes": ["cost_not_covered"],
            "economics": {"candidate": economics, "incumbent": None, "reference": None},
            "screen": {"cost_hurdle": 1.5},
        },
        {},
        params={"fee_bps": 5.0, "slippage_bps": 3.5},
    )
    assert order["budget"]["cost_coverage"] == economics["cost_coverage"]
    assert order["budget"]["cost_hurdle_multiple"] == 1.5
    assert "captured -11.5 bps gross against a 17 bps round trip" in order["diagnosis"]
    assert any("hurdle multiple" in item for item in order["admissible_repairs"])
