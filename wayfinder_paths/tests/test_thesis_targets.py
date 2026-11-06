from unittest.mock import AsyncMock

import pytest

from wayfinder_paths.core.clients.direct.DefiLlamaFreeClient import (
    DefiLlamaFreeClient,
    _enforce_response_budget,
)
from wayfinder_paths.core.theses.checkpoints import ResearchCheckpoint
from wayfinder_paths.core.theses.models import Proposal
from wayfinder_paths.core.theses.publication import validate_proposal
from wayfinder_paths.core.theses.quantification import allocation_key
from wayfinder_paths.core.theses.research import (
    RESEARCH_EVIDENCE_TOOLS,
    execution_readiness,
    research_evidence,
    validate_full_allocation,
    validate_market_capacity,
)
from wayfinder_paths.mcp.tools.thesis_checkpoint import research_thesis_checkpoint


@pytest.fixture
def target():
    return Proposal.model_validate(
        {
            "title": "Target",
            "interpretation": "Research only",
            "intent": "absolute",
            "assumptions": ["No execution"],
            "components": [
                {
                    "id": "a",
                    "title": "A",
                    "rationale": "Fit",
                    "counterargument": "Risk",
                    "invalidation": "Failure",
                    "evidence": ["https://example.test"],
                }
            ],
            "variants": [
                {
                    "budget_usd": b,
                    "cash_bps": 0,
                    "rationale": "Target; quotes pending",
                    "positions": [
                        {
                            "id": "a",
                            "component_id": "a",
                            "kind": "perp",
                            "instrument_id": "BTC-USDC",
                            "symbol": "BTC",
                            "direction": "long",
                            "capital_bps": 10000,
                            "rationale": "Fit",
                        }
                    ],
                }
                for b in (100, 1000, 10000, 100000)
            ],
        }
    )


def test_full_target_does_not_require_a_book_but_does_require_identity(target):
    evidence = research_evidence([{"perps": [{"name": "BTC-USDC"}]}])
    validate_full_allocation(target)
    validate_market_capacity(target, evidence, screen_capacity=False)
    report = execution_readiness(target, evidence)
    assert report["execution_authorized"] is False
    assert all(v["status"] == "execution_pending" for v in report["variants"])
    assert report["variants"][-1]["positions"][0]["capital_usd"] == 100000
    assert report["variants"][-1]["positions"][0]["warnings"]
    with pytest.raises(ValueError, match="exact Hyperliquid"):
        validate_market_capacity(target, {}, screen_capacity=False)


def test_legacy_partial_portfolio_readable_but_not_a_new_target(target):
    payload = target.model_dump()
    payload["variants"][0]["cash_bps"] = 9000
    payload["variants"][0]["positions"][0]["capital_bps"] = 1000
    legacy = Proposal.model_validate(payload)
    with pytest.raises(ValueError, match="10000 bps"):
        validate_full_allocation(legacy)


@pytest.mark.parametrize("capital_bps", [999, 1000])
@pytest.mark.parametrize("instrument", ["ASSET/USDC", "asset-ethereum"])
def test_spot_minimum_is_venue_specific(
    target: Proposal, capital_bps: int, instrument: str
) -> None:
    payload = target.model_dump()
    for variant in payload["variants"]:
        first = variant["positions"][0]
        first["capital_bps"] = 10000 - capital_bps
        variant["positions"].append(
            {
                **first,
                "id": "spot",
                "kind": "token",
                "instrument_id": instrument,
                "symbol": "ASSET",
                "capital_bps": capital_bps,
            }
        )
    proposal = Proposal.model_validate(payload)
    evidence = research_evidence(
        [
            {"perps": [{"name": "BTC-USDC"}], "spots": [{"name": "ASSET/USDC"}]},
            {
                "token_id": "asset-ethereum",
                "address": "0x123",
                "chain": {"code": "ethereum"},
                "identity": {"is_canonical": False, "verification": "unverified"},
            },
        ]
    )
    evidence["fetched_urls"] = ["https://example.test"]
    evidence["quantified_allocations"] = {
        allocation_key(v): {"coverage": "unavailable"} for v in proposal.variants
    }
    if instrument == "ASSET/USDC" and capital_bps < 1000:
        with pytest.raises(ValueError, match="Hyperliquid spot notional"):
            validate_proposal(payload, evidence)
    else:
        assert validate_proposal(payload, evidence) == proposal
        report = execution_readiness(proposal, evidence)
        assert report["execution_authorized"] is False
        assert all(v["status"] == "execution_pending" for v in report["variants"])


@pytest.mark.asyncio
async def test_checkpoint_cannot_supply_evidence_or_execution_authority(monkeypatch):
    monkeypatch.setattr(
        "wayfinder_paths.mcp.utils._report_tool_metric", lambda *_: None
    )
    checkpoint = ResearchCheckpoint.model_validate(
        {
            "stage": "interpretation",
            "spec": {
                "objective": "Growth",
                "horizon": "6 months",
                "constraints": [],
                "mechanisms": ["Demand"],
                "causal_chain": "Demand to revenue",
                "counterfactual": "Dilution",
                "baseline": "Direct exposure",
            },
        }
    )
    response = await research_thesis_checkpoint(checkpoint)
    assert response["ok"]
    assert response["result"]["execution_authorized"] is False
    assert response["result"]["evidence_verified"] is False
    assert "research_thesis_checkpoint" not in RESEARCH_EVIDENCE_TOOLS


@pytest.mark.asyncio
async def test_category_search_filters_before_paging_and_preserves_metadata():
    client = DefiLlamaFreeClient()
    protocols = [
        {
            "name": str(i),
            "category": "Other" if i % 2 else "DEX",
            "description": "Trading",
            "gecko_id": f"id-{i}",
        }
        for i in range(80)
    ]
    client.protocols = AsyncMock(
        side_effect=lambda: {
            "url": "https://api.llama.fi/protocols",
            "result": protocols,
        }
    )
    first = (await client.protocol_search("_", 30, category="dex"))["result"]
    assert first["count"] == 30
    assert first["page"]["totalAvailable"] == 40
    second = (
        await client.protocol_search(
            "_", 30, category="DEX", cursor=first["page"]["nextCursor"]
        )
    )["result"]
    assert second["count"] == 10
    assert second["matches"][0]["gecko_id"] == "id-60"
    assert second["matches"][0]["description"] == "Trading"
    assert second["page"]["nextCursor"] is None


def test_oversized_single_catalog_item_cannot_loop_forever():
    result = _enforce_response_budget(
        {"result": {"items": [{"payload": "x" * 300000}]}}
    )
    assert result["result"]["truncated"]
    assert result["result"]["items"] == []
