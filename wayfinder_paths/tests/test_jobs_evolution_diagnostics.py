from __future__ import annotations

import json

import pytest

from wayfinder_paths.jobs.evolution_diagnostics import (
    DIAGNOSTIC_PACK_MAX_BYTES,
    attempt_made_progress,
    build_diagnostic_pack,
    build_postmortem,
    compact_postmortem,
    participation_adjusted_score,
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


def test_participation_adjustment_does_not_reward_sparse_non_trading() -> None:
    assert participation_adjusted_score(0.1, trade_count=3, target_trades=12) == (
        pytest.approx(0.025)
    )
    assert participation_adjusted_score(-0.1, trade_count=3, target_trades=12) == (
        pytest.approx(-0.4)
    )
    assert participation_adjusted_score(0.1, trade_count=0) == float("-inf")
