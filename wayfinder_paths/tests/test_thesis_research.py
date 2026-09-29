from typing import Any

import pytest

from wayfinder_paths.core.theses.models import Proposal
from wayfinder_paths.core.theses.research import (
    prediction_evidence,
    validate_prediction_capacity,
)


@pytest.fixture
def proposal() -> Proposal:
    return Proposal.model_validate(
        {
            "title": "No rate hike",
            "interpretation": "Research only",
            "intent": "absolute",
            "assumptions": ["No execution"],
            "components": [
                {
                    "id": "policy",
                    "title": "Policy",
                    "rationale": "Thesis",
                    "counterargument": "Inflation",
                    "invalidation": "A hike",
                    "evidence": ["https://example.com"],
                }
            ],
            "variants": [
                {
                    "budget_usd": budget,
                    "rationale": "Depth limited",
                    "cash_bps": 9800,
                    "positions": [
                        {
                            "id": "no",
                            "component_id": "policy",
                            "kind": "prediction",
                            "instrument_id": "123",
                            "symbol": "NO",
                            "direction": "no",
                            "capital_bps": 200,
                            "rationale": "Verified outcome",
                        }
                    ],
                }
                for budget in (100, 1000, 10000, 100000)
            ],
        }
    )


def test_observed_outcomes_and_latest_book_not_market_liquidity() -> None:
    evidence = prediction_evidence(
        [
            {
                "candidates": [
                    {
                        "eventSlug": "fed",
                        "tradable": True,
                        "liquidity": 1_000_000,
                        "outcomes": [{"label": "No", "tokenId": "123"}],
                    }
                ]
            },
            {
                "action": "order_book",
                "token_id": "123",
                "book": {"topAskNotional": 50000},
            },
            {
                "action": "order_book",
                "token_id": "123",
                "book": {"topAskNotional": 20000},
            },
        ]
    )
    assert evidence == {
        "outcomes": {"123": "no"},
        "ask_depth": {"123": 20000},
        "event_urls": ["https://polymarket.com/event/fed"],
    }


def test_later_closed_market_invalidates_outcome() -> None:
    candidate = {"tradable": True, "outcomes": [{"label": "No", "tokenId": "123"}]}
    evidence = prediction_evidence(
        [
            {"candidates": [candidate]},
            {"candidates": [{**candidate, "tradable": False}]},
        ]
    )
    assert evidence["outcomes"] == {}


def test_raw_book_uses_same_three_best_asks_as_summary() -> None:
    evidence = prediction_evidence(
        [
            {
                "action": "order_book",
                "token_id": "123",
                "book": {
                    "asks": [
                        {"price": "0.99", "size": "1000000"},
                        {"price": "0.17", "size": "100"},
                        {"price": "0.16", "size": "100"},
                        {"price": "0.15", "size": "100"},
                    ],
                },
            }
        ]
    )
    assert evidence["ask_depth"]["123"] == 48


def test_single_summary_market_can_verify_outcome() -> None:
    evidence = prediction_evidence(
        [
            {
                "action": "get_market",
                "summaryMode": True,
                "market": {
                    "tradable": True,
                    "outcomes": [{"label": "No", "tokenId": "123"}],
                },
            }
        ]
    )
    assert evidence["outcomes"] == {"123": "no"}


@pytest.mark.parametrize(
    "evidence",
    [
        {},
        {"outcomes": {"123": "yes"}, "ask_depth": {"123": 20000}},
        {"outcomes": {"123": "no"}},
        {"outcomes": {"123": "no"}, "ask_depth": {"123": 19999}},
    ],
)
def test_unverified_or_oversized_prediction_rejected(
    proposal: Proposal, evidence: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        validate_prediction_capacity(proposal, evidence)


def test_capacity_uses_numeric_allocation_at_each_budget(proposal: Proposal) -> None:
    evidence = {"outcomes": {"123": "no"}, "ask_depth": {"123": 20000}}
    validate_prediction_capacity(proposal, evidence)
    payload = proposal.model_dump()
    payload["variants"][-1]["positions"][0].update(
        capital_bps=1800, rationale="This is only $1,800 so it fits"
    )
    payload["variants"][-1]["cash_bps"] = 8200
    with pytest.raises(ValueError, match=r"capital \$18000 exceeds"):
        validate_prediction_capacity(Proposal.model_validate(payload), evidence)
