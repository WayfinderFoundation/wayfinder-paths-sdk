from typing import Any

import pytest

from wayfinder_paths.core.theses.models import Proposal
from wayfinder_paths.core.theses.research import (
    missing_source_reads,
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
        "fetched_urls": [],
        "hyperliquid_depth": {},
        "onchain_tokens": {},
        "onchain_pools": {},
    }


@pytest.mark.parametrize(
    "result,expected",
    [
        (
            {"results": [{"url": "https://example.com", "contentExcerpt": "Policy"}]},
            ["https://example.com"],
        ),
        ({"results": [{"url": "https://example.com", "contentExcerpt": "  "}]}, []),
        ({"results": [{"url": "https://example.com", "error": "unavailable"}]}, []),
        (
            {
                "action": "search",
                "candidates": [{"eventSlug": "fed", "description": "Policy"}],
            },
            [],
        ),
        (
            {
                "action": "get_event",
                "event": {"slug": "fed", "description": "Resolution rules"},
            },
            ["https://polymarket.com/event/fed"],
        ),
        (
            {
                "action": "get_market",
                "market": {"eventSlug": "fed", "rules": "Resolution rules"},
                "summaryMode": True,
            },
            ["https://polymarket.com/event/fed"],
        ),
        (
            {
                "action": "get_market",
                "market": {"eventSlug": "fed"},
                "summaryMode": True,
            },
            [],
        ),
    ],
)
def test_source_reads_require_returned_text(
    result: dict[str, Any], expected: list[str]
) -> None:
    assert research_evidence([result])["fetched_urls"] == expected


def test_source_reads_only_require_invested_components(proposal: Proposal) -> None:
    assert missing_source_reads(proposal, {}) == ["policy"]
    assert missing_source_reads(
        proposal, {"fetched_urls": ["https://unrelated.test"]}
    ) == ["policy"]
    assert (
        missing_source_reads(proposal, {"fetched_urls": ["https://example.com"]}) == []
    )
    # Extra research components without an allocation do not require a fetch.
    extra = proposal.components[0].model_copy(update={"id": "alternative"})
    proposal = proposal.model_copy(update={"components": [*proposal.components, extra]})
    assert (
        missing_source_reads(proposal, {"fetched_urls": ["https://example.com"]}) == []
    )
    proposal = proposal.model_copy(
        update={
            "variants": [
                v.model_copy(update={"positions": [], "cash_bps": 10000})
                for v in proposal.variants
            ]
        }
    )
    assert missing_source_reads(proposal, {}) == []


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


@pytest.fixture
def onchain_case(proposal: Proposal) -> tuple[Proposal, list[dict[str, Any]]]:
    address = "0x" + "a" * 40
    token_id = f"ethereum_{address}"
    payload = proposal.model_dump()
    for variant in payload["variants"]:
        variant["positions"][0].update(
            kind="token", instrument_id=token_id, direction="long"
        )
    return Proposal.model_validate(payload), [
        {
            "token_id": token_id,
            "address": address,
            "chain": {"code": "ethereum", "id": 1},
            "identity": {"is_canonical": False, "suspicious": False},
            "links": {"homepage": ["https://project.test/"]},
        },
        {
            "results": [
                {
                    "url": "https://docs.project.test/contracts",
                    "contentExcerpt": f"Ethereum token: {address}",
                }
            ]
        },
        {
            "chain_code": "ethereum",
            "tokens": [
                {
                    "token_id": token_id,
                    "chain_code": "ethereum",
                    "address": address,
                    "pool_address": "0x" + "b" * 40,
                    "liquidity_usd": 400000,
                    "volume_24h_usd": 200000,
                }
            ],
        },
    ]


def test_onchain_evidence_corroborates_contract_and_caps_local_pool(
    onchain_case: tuple[Proposal, list[dict[str, Any]]],
) -> None:
    proposal, results = onchain_case
    evidence = research_evidence(results)
    validate_market_capacity(proposal, evidence)
    token_id = proposal.variants[0].positions[0].instrument_id
    assert (
        evidence["onchain_tokens"][token_id]["issuer_reference"]
        == "https://docs.project.test/contracts"
    )
    assert "links" not in evidence["onchain_tokens"][token_id]
    assert "pages" not in evidence
    assert evidence["onchain_pools"][token_id]["pool_address"] == "0x" + "b" * 40


def test_capacity_reports_all_instruments_and_largest_failing_budget(
    onchain_case: tuple[Proposal, list[dict[str, Any]]],
) -> None:
    proposal, results = onchain_case
    payload = proposal.model_dump()
    unknown_id = "ethereum_0x" + "c" * 40
    for variant in payload["variants"]:
        variant["cash_bps"] -= 200
        variant["positions"].append(
            {**variant["positions"][0], "id": "typo", "instrument_id": unknown_id}
        )
    # A missing issuer proof must not hide an unrelated address transcription error.
    evidence = research_evidence([results[0], results[2]])
    with pytest.raises(ValueError) as error:
        validate_market_capacity(Proposal.model_validate(payload), evidence)
    failures = str(error.value).splitlines()
    assert len(failures) == 2
    assert failures[0].startswith("100000/no:")
    assert "registry-linked issuer" in failures[0]
    assert failures[1].startswith("100000/typo:")
    assert f"unknown onchain instrument_id {unknown_id!r}" in failures[1]


@pytest.mark.parametrize(
    "failure",
    [
        "unresolved",
        "no_page",
        "wrong_host",
        "host_prefix",
        "wrong_address",
        "address_prefix",
        "suspicious",
    ],
)
def test_onchain_identity_cannot_be_replaced_with_a_disclaimer(
    onchain_case: tuple[Proposal, list[dict[str, Any]]],
    failure: str,
) -> None:
    proposal, results = onchain_case
    page = results[1]["results"][0]
    if failure == "unresolved":
        results[0] = {}
    elif failure == "no_page":
        results[1] = {}
    elif failure == "wrong_host":
        page["url"] = "https://exchange.test/listing"
    elif failure == "host_prefix":
        page["url"] = "https://project.test.attacker.test/contracts"
    elif failure == "wrong_address":
        page["contentExcerpt"] = "0x" + "b" * 40
    elif failure == "address_prefix":
        page["contentExcerpt"] += "a"
    else:
        results[0]["identity"]["suspicious"] = True
    expected = (
        "unknown onchain instrument_id"
        if failure == "unresolved"
        else "registry-linked issuer"
    )
    with pytest.raises(ValueError, match=expected):
        validate_market_capacity(proposal, research_evidence(results))


def test_canonical_identity_needs_no_issuer_fetch_but_still_needs_pool(
    onchain_case: tuple[Proposal, list[dict[str, Any]]],
) -> None:
    proposal, results = onchain_case
    results[0]["identity"]["is_canonical"] = True
    results[1] = {}
    validate_market_capacity(proposal, research_evidence(results))
    with pytest.raises(ValueError, match="sizing proxy"):
        validate_market_capacity(proposal, research_evidence(results[:2]))


@pytest.mark.parametrize("native", [False, True])
def test_only_registry_native_identity_can_use_wrapped_pool(
    onchain_case: tuple[Proposal, list[dict[str, Any]]], native: bool
) -> None:
    proposal, results = onchain_case
    wrapped = "0x" + "c" * 40
    results[0]["identity"].update(
        is_canonical=True,
        verification="native" if native else "issuer",
        wrapped_native_address=wrapped,
    )
    results[2]["tokens"][0].update(token_id=f"ethereum_{wrapped}", address=wrapped)
    evidence = research_evidence(results)
    if native:
        validate_market_capacity(proposal, evidence)
        assert proposal.variants[0].positions[0].instrument_id == results[0]["token_id"]
        results[2]["tokens"][0]["chain_code"] = "base"
        with pytest.raises(ValueError, match="sizing proxy"):
            validate_market_capacity(proposal, research_evidence(results))
    else:
        with pytest.raises(ValueError, match="sizing proxy"):
            validate_market_capacity(proposal, evidence)


@pytest.mark.parametrize(
    "field,value",
    [
        ("liquidity_usd", 399999),
        ("volume_24h_usd", 199999),
        ("volume_24h_usd", float("nan")),
        ("liquidity_usd", float("inf")),
        ("liquidity_usd", None),
        ("chain_code", "base"),
        ("address", "0x" + "b" * 40),
    ],
)
def test_onchain_pool_cap_cannot_use_global_volume_or_another_token(
    onchain_case: tuple[Proposal, list[dict[str, Any]]], field: str, value: Any
) -> None:
    proposal, results = onchain_case
    results[0]["total_volume_usd_24h"] = 1e12
    results[2]["tokens"][0][field] = value
    with pytest.raises(ValueError, match="sizing proxy"):
        validate_market_capacity(proposal, research_evidence(results))


def test_sol_mint_and_registry_homepage_path_are_case_sensitive(
    onchain_case: tuple[Proposal, list[dict[str, Any]]],
) -> None:
    _, results = onchain_case
    mint = "AbCd" * 8
    results[0].update(address=mint, links={"homepage": ["https://shared.test/project"]})
    page = results[1]["results"][0]
    page.update(url="https://shared.test/another", contentExcerpt=mint)
    token_id = results[0]["token_id"]
    assert (
        research_evidence(results)["onchain_tokens"][token_id]["issuer_reference"]
        is None
    )
    page["url"] = "https://shared.test/project/contracts"
    assert (
        research_evidence(results)["onchain_tokens"][token_id]["issuer_reference"]
        == page["url"]
    )
    page["contentExcerpt"] = mint.lower()
    assert (
        research_evidence(results)["onchain_tokens"][token_id]["issuer_reference"]
        is None
    )


@pytest.mark.parametrize(
    "homepage,page_url,allowed",
    [
        ("https://app.project.com", "https://docs.project.com/contracts", True),
        ("https://project.co.uk", "https://other.co.uk/contracts", False),
        ("https://project.github.io", "https://other.github.io/contracts", False),
        ("https://project.github.io", "https://docs.project.github.io/contracts", True),
        (
            "https://github.com/project/contracts",
            "https://github.com/attacker/contracts",
            False,
        ),
        (
            "https://github.com/project/contracts",
            "https://github.com/project/contracts/blob/main/token.sol",
            True,
        ),
    ],
)
def test_issuer_domain_matching_respects_registrable_and_private_suffixes(
    onchain_case: tuple[Proposal, list[dict[str, Any]]],
    homepage: str,
    page_url: str,
    allowed: bool,
) -> None:
    _, results = onchain_case
    results[0]["links"] = {"homepage": [homepage]}
    results[1]["results"][0]["url"] = page_url
    token_id = results[0]["token_id"]
    assert (
        bool(research_evidence(results)["onchain_tokens"][token_id]["issuer_reference"])
        is allowed
    )
