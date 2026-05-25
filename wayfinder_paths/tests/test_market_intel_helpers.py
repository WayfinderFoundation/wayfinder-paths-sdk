from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from wayfinder_paths.core.market_intel_log import (
    append_log,
    freshness_check,
    search_log,
    update_outcome,
)
from wayfinder_paths.quant.market_metrics import (
    beta,
    funding_adjusted_returns,
    max_drawdown,
    sharpe,
    turnover_cost,
)
from wayfinder_paths.quant.polymarket_edge import (
    apply_log_odds_update,
    binary_kelly,
    binary_no_ev,
    binary_yes_ev,
    normalize_binary_prices,
    roi,
    simple_annualized_roi,
    sweep_asks,
)


def test_polymarket_edge_binary_math() -> None:
    assert binary_yes_ev(0.55, 0.48) == pytest.approx(0.07)
    assert binary_no_ev(0.55, 0.40) == pytest.approx(0.05)
    assert roi(0.07, 0.48) == pytest.approx(0.1458333333)
    assert binary_kelly(0.55, 0.48) == pytest.approx(0.1346153846)
    assert simple_annualized_roi(0.05, 30) == pytest.approx(0.810519, rel=1e-4)


def test_polymarket_edge_order_book_prior_and_log_odds_update() -> None:
    normalized = normalize_binary_prices(0.45, 0.58)
    assert normalized["marketPrior"] == pytest.approx(0.4368932039)
    assert normalized["spreadCost"] == pytest.approx(0.03)

    sweep = sweep_asks(
        [{"price": 0.45, "size": 10}, {"price": 0.50, "size": 20}],
        target_notional=10,
    )
    assert sweep["filled"] is True
    assert sweep["levelsUsed"] == 2
    assert sweep["notional"] == pytest.approx(10)
    assert sweep["avgPrice"] == pytest.approx(0.4761904762)

    posterior = apply_log_odds_update(0.40, [0.25, -0.10])
    assert posterior == pytest.approx(0.43647881)


def test_market_metrics_helpers() -> None:
    assert max_drawdown([100, 120, 90, 110]) == pytest.approx(-0.25)
    assert sharpe([0.01, 0.02, -0.01], periods_per_year=3) == pytest.approx(0.92582)
    assert beta([0.01, 0.02, 0.03], [0.02, 0.04, 0.06]) == pytest.approx(0.5)
    assert funding_adjusted_returns([0.01, 0.02], [-0.001, 0.002]) == [
        pytest.approx(0.009),
        pytest.approx(0.022),
    ]
    assert turnover_cost([1.0, 0.5], fee_bps=5, slippage_bps=2) == [
        pytest.approx(0.0007),
        pytest.approx(0.00035),
    ]


def test_market_intel_log_append_search_update_and_freshness(tmp_path) -> None:
    log_dir = tmp_path / ".wayfinder_runs"
    expires_at = (datetime.now(UTC) + timedelta(minutes=15)).isoformat()
    entry = append_log(
        {
            "producer": "wayfinder-research",
            "kind": "forecast_case",
            "subject": {"venue": "polymarket", "marketId": "abc"},
            "observedAt": datetime.now(UTC).isoformat(),
            "expiresAt": expires_at,
            "summary": "Market prior 40%, posterior 45%.",
            "mustRehydrate": ["price", "order_book"],
        },
        path=log_dir,
    )

    assert entry["schemaVersion"] == "wf.market_intel_log.v1"
    assert entry["safeToReuseWithoutRehydration"] is False

    matches = search_log(
        subject={"venue": "polymarket"},
        kind="forecast_case",
        path=log_dir,
    )
    assert [match["id"] for match in matches] == [entry["id"]]

    freshness = freshness_check(entry)
    assert freshness["isFresh"] is True
    assert freshness["safeToReuseWithoutRehydration"] is False
    assert freshness["mustRehydrate"] == ["price", "order_book"]

    outcome = update_outcome(entry["id"], {"realizedOutcome": "YES"}, path=log_dir)
    assert outcome["kind"] == "outcome_update"
    assert outcome["outcome"]["entryId"] == entry["id"]
