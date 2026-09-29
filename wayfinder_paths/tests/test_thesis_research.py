from typing import Any

import pytest

from wayfinder_paths.core.theses.models import Proposal
from wayfinder_paths.core.theses.research import (
    research_evidence,
    validate_market_capacity,
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
    evidence = research_evidence(
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
        "hyperliquid_depth": {},
    }


def test_later_closed_market_invalidates_outcome() -> None:
    candidate = {"tradable": True, "outcomes": [{"label": "No", "tokenId": "123"}]}
    evidence = research_evidence(
        [
            {"candidates": [candidate]},
            {"candidates": [{**candidate, "tradable": False}]},
        ]
    )
    assert evidence["outcomes"] == {}


def test_raw_book_uses_same_three_best_asks_as_summary() -> None:
    evidence = research_evidence(
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
    evidence = research_evidence(
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
        validate_market_capacity(proposal, evidence)


def test_capacity_uses_numeric_allocation_at_each_budget(proposal: Proposal) -> None:
    evidence = {"outcomes": {"123": "no"}, "ask_depth": {"123": 20000}}
    validate_market_capacity(proposal, evidence)
    payload = proposal.model_dump()
    payload["variants"][-1]["positions"][0].update(
        capital_bps=1800, rationale="This is only $1,800 so it fits"
    )
    payload["variants"][-1]["cash_bps"] = 8200
    with pytest.raises(ValueError, match=r"capital \$18000 exceeds"):
        validate_market_capacity(Proposal.model_validate(payload), evidence)


@pytest.mark.parametrize(
    "kind,instrument,leverage",
    [("token", "HYPE/USDC", 1), ("perp", "HYPE-USDC", 2), ("hip3", "xyz:GOLD", 1)],
)
def test_hyperliquid_capacity_uses_smaller_book_and_notional(
    proposal, kind, instrument, leverage
):
    payload = proposal.model_dump()
    for variant in payload["variants"]:
        variant["positions"][0].update(
            kind=kind, instrument_id=instrument, direction="long", leverage=leverage
        )
    proposal = Proposal.model_validate(payload)
    book = {"bid_notional_usd_50bps": 40000, "ask_notional_usd_50bps": 50000}
    evidence = research_evidence([{"depth": {instrument: book}}])
    validate_market_capacity(proposal, evidence)
    evidence["hyperliquid_depth"][instrument] = {
        **book,
        "bid_notional_usd_50bps": 10000,
    }
    with pytest.raises(ValueError, match="observed two-sided depth"):
        validate_market_capacity(proposal, evidence)


@pytest.mark.parametrize(
    "bid,ask", [(0, 10000), (10000, 0), (float("nan"), 10000), (float("inf"), 10000)]
)
def test_missing_or_invalid_hyperliquid_depth_fails_closed(proposal, bid, ask):
    payload = proposal.model_dump()
    payload["variants"][0]["positions"][0].update(
        kind="perp", instrument_id="BTC-USDC", direction="short"
    )
    with pytest.raises(ValueError, match="Hyperliquid notional"):
        validate_market_capacity(
            Proposal.model_validate(payload),
            {
                "hyperliquid_depth": {
                    "BTC-USDC": {
                        "bid_notional_usd_50bps": bid,
                        "ask_notional_usd_50bps": ask,
                    }
                }
            },
        )


def test_latest_hyperliquid_book_replaces_older_deeper_snapshot():
    evidence = research_evidence(
        [
            {"depth": {"HYPE/USDC": {"bid_notional_usd_50bps": 50000}}},
            {"depth": {"HYPE/USDC": {"bid_notional_usd_50bps": 500}}},
        ]
    )
    assert evidence["hyperliquid_depth"]["HYPE/USDC"]["bid_notional_usd_50bps"] == 500
