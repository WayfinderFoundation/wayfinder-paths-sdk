from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from wayfinder_paths.core.market_intel_log import (
    append_log,
    freshness_check,
    latest_for_subject,
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
    bayes_update_from_evidence,
    binary_kelly,
    binary_no_ev,
    binary_yes_ev,
    brier_score,
    compounded_annualized_roi,
    conservative_trade_gate,
    evidence_llr,
    implied_prior_from_quote,
    log_loss,
    normalize_binary_prices,
    posterior_band_from_evidence,
    reprice_forecast_from_quote,
    roi,
    signed_binary_kelly,
    simple_annualized_roi,
    sweep_asks,
)


def test_polymarket_edge_binary_math() -> None:
    assert binary_yes_ev(0.55, 0.48) == pytest.approx(0.07)
    assert binary_no_ev(0.55, 0.40) == pytest.approx(0.05)
    assert roi(0.07, 0.48) == pytest.approx(0.1458333333)
    assert binary_kelly(0.55, 0.48) == pytest.approx(0.1346153846)
    assert binary_kelly(0.40, 0.48) == 0.0
    assert signed_binary_kelly(0.40, 0.48) == pytest.approx(-0.1538461538)
    assert simple_annualized_roi(0.05, 30) == pytest.approx(0.6083333333)
    assert compounded_annualized_roi(0.05, 30) == pytest.approx(
        0.810519,
        rel=1e-4,
    )


def test_polymarket_edge_order_book_prior_and_log_odds_update() -> None:
    normalized = normalize_binary_prices(0.45, 0.58)
    assert normalized["priorSource"] == "normalized_binary_prices"
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


def test_polymarket_quote_prior_and_evidence_cards() -> None:
    prior = implied_prior_from_quote(yes_bid=0.40, yes_ask=0.46)
    assert prior["marketPrior"] == pytest.approx(0.43)
    assert prior["priorSource"] == "bid_ask_mid"
    assert prior["quality"] == "medium"
    assert prior["isExecutable"] is True

    assert implied_prior_from_quote(None, 0.47)["priorSource"] == "ask_only"
    assert implied_prior_from_quote(0.41, None)["isExecutable"] is False
    last_trade_context = implied_prior_from_quote(None, None, last=0.44)
    assert last_trade_context["quality"] == "very_low"
    assert last_trade_context["priorSource"] == "last_trade_context_only"
    assert last_trade_context["marketPrior"] is None
    assert last_trade_context["p"] is None
    assert last_trade_context["lastTrade"] == pytest.approx(0.44)
    assert last_trade_context["isExecutable"] is False

    pro_card = {
        "claim": "Primary source confirms the event before resolution.",
        "direction": "for_yes",
        "strength": "strong",
        "sourceQuality": "primary",
        "freshness": "fresh",
        "independence": "independent",
        "alreadyPriced": "unlikely",
        "resolutionRelevance": "direct",
    }
    anti_card = {
        "claim": "Secondary report partially conflicts with timing.",
        "direction": "against_yes",
        "strength": "medium",
        "sourceQuality": "reputable_secondary",
        "freshness": "recent",
        "independence": "independent",
        "alreadyPriced": "maybe",
        "resolutionRelevance": "direct",
    }

    assert evidence_llr(pro_card) == pytest.approx(0.5)
    assert evidence_llr(anti_card) == pytest.approx(-0.096)

    update = bayes_update_from_evidence(
        0.40,
        [pro_card, anti_card],
        max_abs_log_odds_move=0.30,
    )
    assert update["rawLogOddsMove"] == pytest.approx(0.404)
    assert update["cappedLogOddsMove"] == pytest.approx(0.30)
    assert update["pBase"] == pytest.approx(0.473657, rel=1e-5)
    assert update["evidenceCards"][0]["computedLlr"] == pytest.approx(0.5)

    band = posterior_band_from_evidence(0.40, [pro_card, anti_card])
    assert band["pLow"] < band["pBase"] < band["pHigh"]


def test_polymarket_conservative_trade_gate() -> None:
    yes_gate = conservative_trade_gate(
        side="BUY_YES",
        p_low=0.48,
        p_base=0.55,
        p_high=0.60,
        entry=0.46,
        min_ev=0.02,
    )
    assert yes_gate["passes"] is True
    assert yes_gate["baseEv"] == pytest.approx(0.09)
    assert yes_gate["conservativeEv"] == pytest.approx(0.02)

    blocked = conservative_trade_gate(
        side="YES",
        p_low=0.47,
        p_base=0.55,
        p_high=0.60,
        entry=0.46,
        min_ev=0.02,
    )
    assert blocked["passes"] is False

    no_gate = conservative_trade_gate(
        side="NO",
        p_low=0.40,
        p_base=0.47,
        p_high=0.52,
        entry=0.45,
        min_ev=0.02,
    )
    assert no_gate["passes"] is True
    assert no_gate["conservativeEv"] == pytest.approx(0.03)

    reprice = reprice_forecast_from_quote(
        p_low=0.48,
        p_base=0.55,
        p_high=0.60,
        yes_bid=0.40,
        yes_ask=0.46,
        min_ev=0.02,
    )
    assert reprice["marketPrior"]["marketPrior"] == pytest.approx(0.43)
    assert reprice["entryYes"] == 0.46
    assert reprice["entryNo"] == pytest.approx(0.60)
    assert reprice["decision"] == "BUY_YES_CANDIDATE"

    watch = reprice_forecast_from_quote(
        p_low=0.47,
        p_base=0.50,
        p_high=0.53,
        yes_bid=0.40,
        yes_ask=0.49,
        min_ev=0.02,
    )
    assert watch["decision"] == "WATCH"

    assert brier_score(0.70, True) == pytest.approx(0.09)
    assert log_loss(0.70, True) == pytest.approx(0.3566749439)


def test_market_metrics_helpers() -> None:
    assert max_drawdown([100, 120, 90, 110]) == pytest.approx(-0.25)
    assert sharpe([0.01, 0.02, -0.01], periods_per_year=3) == pytest.approx(0.92582)
    assert beta([0.01, 0.02, 0.03], [0.02, 0.04, 0.06]) == pytest.approx(0.5)
    assert funding_adjusted_returns([0.01, 0.02], [-0.001, 0.002]) == [
        pytest.approx(0.011),
        pytest.approx(0.018),
    ]
    assert funding_adjusted_returns(
        [0.01, 0.02],
        [-0.001, 0.002],
        side="short",
    ) == [
        pytest.approx(-0.011),
        pytest.approx(-0.018),
    ]
    with pytest.raises(ValueError, match="side must be"):
        funding_adjusted_returns([0.01], [0.001], side="flat")
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
    assert entry["artifactRefs"] == []
    assert entry["sources"] == []
    assert entry["outcome"] is None
    assert entry["parentId"] is None
    assert entry["relatedLogIds"] == []

    matches = search_log(
        subject={"venue": "polymarket"},
        kind="forecast_case",
        path=log_dir,
    )
    assert [match["id"] for match in matches] == [entry["id"]]

    second = append_log(
        {
            "producer": "wayfinder-research",
            "kind": "quote_update",
            "subject": {"venue": "polymarket", "marketId": "abc"},
            "observedAt": datetime.now(UTC).isoformat(),
            "expiresAt": expires_at,
            "summary": "Quote improved; posterior unchanged.",
            "parentId": entry["id"],
            "relatedLogIds": [entry["id"]],
            "state": {"previousPBase": 0.49, "newEntryYes": 0.42},
            "mustRehydrate": ["order_book", "liquidity", "news"],
        },
        path=log_dir,
    )
    latest = latest_for_subject(
        {"venue": "polymarket", "marketId": "abc"},
        path=log_dir,
    )
    assert latest is not None
    assert latest["id"] == second["id"]

    freshness = freshness_check(entry)
    assert freshness["isFresh"] is True
    assert freshness["safeToReuseWithoutRehydration"] is False
    assert freshness["mustRehydrate"] == ["price", "order_book"]
    assert freshness["reuseMode"] == "assumption_seed"

    no_expiry = freshness_check({"safeToReuseWithoutRehydration": True})
    assert no_expiry["isFresh"] is False
    assert no_expiry["safeToReuseWithoutRehydration"] is False
    assert no_expiry["reuseMode"] == "audit_only"

    outcome = update_outcome(entry["id"], {"realizedOutcome": "YES"}, path=log_dir)
    assert outcome["kind"] == "outcome_update"
    assert outcome["parentId"] == entry["id"]
    assert outcome["relatedLogIds"] == [entry["id"]]
    assert outcome["outcome"]["entryId"] == entry["id"]
