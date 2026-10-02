import hashlib
import json
from copy import deepcopy

import pytest

from wayfinder_paths.core.theses.assessment import (
    CHECKPOINT_TOOL,
    checkpoints,
    research_notebook,
)
from wayfinder_paths.core.theses.checkpoints import ResearchCheckpoint, ReviewCheckpoint
from wayfinder_paths.core.theses.draft import (
    draft_context,
    draft_status,
    publication_result,
)
from wayfinder_paths.core.theses.models import Construction, Variant
from wayfinder_paths.core.theses.quantification import (
    DAY_MS,
    allocation_key,
    quantify_variants,
)
from wayfinder_paths.core.theses.review import REVIEW_TOOL
from wayfinder_paths.core.theses.sizing import construction_errors, size_variant
from wayfinder_paths.tests import test_thesis_assessment, test_thesis_targets

spec = test_thesis_assessment.spec
target = test_thesis_targets.target


def test_v5_requires_review_receipt_and_implementation_comparisons(run):
    parent, child = run
    for index, message in enumerate(parent):
        for part in message.get("parts", []):
            if part.get("tool") == CHECKPOINT_TOOL:
                cp = part["state"]["input"]["checkpoint"]
                cp["schema_version"] = 5
                parent[index] = receipt(cp, part["state"]["time"]["end"])
    status = draft_status(parent, child)
    assert any("research_thesis_review" in e for e in status["errors"])
    assert any("Compare selected implementations" in e for e in status["errors"])
    cp = ReviewCheckpoint(findings=[])
    child.append(
        {
            "info": {
                "agent": "thesis-reviewer",
                "sessionID": "reviewer",
                "finish": "stop",
            },
            "parts": [
                {
                    "tool": REVIEW_TOOL,
                    "state": {
                        "status": "completed",
                        "time": {"end": 20},
                        "input": {"checkpoint": cp.model_dump()},
                        "output": json.dumps(
                            {
                                "ok": True,
                                "result": {
                                    "sha256": hashlib.sha256(
                                        cp.model_dump_json().encode()
                                    ).hexdigest()
                                },
                            }
                        ),
                    },
                }
            ],
        }
    )
    parent.append(
        observation(
            "wayfinder_research_quantify_portfolio",
            {
                "implementation_comparisons": {
                    "BTC-USDC": {
                        "observations": {
                            "depth": {"BTC-USDC": {"ask_notional_usd_50bps": 100000}}
                        }
                    }
                }
            },
            21,
        )
    )
    assert draft_status(parent, child)["ready"]


def receipt(payload, number, session="parent", agent="thesis-research"):
    cp = ResearchCheckpoint.model_validate(payload)
    return {
        "info": {"id": f"m{number}", "sessionID": session, "agent": agent},
        "parts": [
            {
                "id": f"t{number}",
                "type": "tool",
                "tool": CHECKPOINT_TOOL,
                "state": {
                    "status": "completed",
                    "time": {"end": number},
                    "input": {"checkpoint": cp.model_dump()},
                    "output": json.dumps(
                        {
                            "ok": True,
                            "result": {
                                "sha256": hashlib.sha256(
                                    cp.receipt_json().encode()
                                ).hexdigest()
                            },
                        }
                    ),
                },
            }
        ],
    }


def observation(tool, result, number):
    return {
        "info": {"sessionID": "parent"},
        "parts": [
            {
                "type": "tool",
                "tool": tool,
                "state": {
                    "status": "completed",
                    "time": {"end": number},
                    "output": json.dumps({"ok": True, "result": result}),
                },
            }
        ],
    }


@pytest.fixture
def run(target, spec):
    construction = {"mode": "directional"}
    base = {"schema_version": 4, "construction": construction}
    parent = [receipt({**base, "stage": "interpretation", "spec": spec}, 1)]
    metadata = target.model_dump(exclude={"variants", "components"})
    parent.append(
        receipt(
            {
                **base,
                "stage": "draft",
                "draft": {
                    "metadata": metadata,
                    "components": [c.model_dump() for c in target.components],
                },
            },
            2,
        )
    )
    for index, variant in enumerate(target.variants, 3):
        parent.append(
            receipt(
                {**base, "stage": "draft", "draft": {"variant": variant.model_dump()}},
                index,
            )
        )
    case = {
        "entity": "bitcoin",
        "name": "Bitcoin",
        "mechanism": "Fixture",
        "observed_identifiers": ["BTC-USDC"],
        "sources": ["https://example.test"],
        "instruments": ["BTC-USDC"],
        "effect_order": 1,
        "value_capture": "Fixture",
        "support": "Fixture",
        "counterevidence": "Fixture",
        "closest_alternative": "Fixture",
        "gaps": [],
        "case_basis": "economic",
        "decision": "KEEP",
        "decision_basis": "economic",
        "reason": "Fixture",
    }
    parent.append(receipt({**base, "stage": "judged", "candidates": [case]}, 7))
    parent.extend(
        [
            observation(
                "wayfinder_hyperliquid_search_market",
                {"perps": [{"name": "BTC-USDC"}]},
                8,
            ),
            observation(
                "wayfinder_core_web_fetch",
                {
                    "results": [
                        {"url": "https://example.test", "contentExcerpt": "Fixture"}
                    ]
                },
                9,
            ),
            observation(
                "wayfinder_research_quantify_portfolio",
                {
                    "portfolio_quantification": {
                        "portfolios": [
                            {"allocation_key": allocation_key(v)}
                            for v in target.variants
                        ]
                    }
                },
                10,
            ),
        ]
    )
    child = [
        {
            "info": {
                "sessionID": "reviewer",
                "agent": "thesis-reviewer",
                "finish": "stop",
            },
            "parts": [
                {
                    "type": "tool",
                    "tool": "thesis_notebook",
                    "state": {
                        "status": "completed",
                        "output": json.dumps(
                            research_notebook(parent, [], entities=["bitcoin"])
                        ),
                    },
                }
            ],
        }
    ]
    return parent, child


def test_incremental_draft_reference_and_legacy_response(run, target):
    parent, child = run
    status = draft_status(parent, child, include_proposal=True)
    assert status["ready"], status["errors"]
    assert status["proposal"] == target.model_dump()
    result = publication_result(
        json.dumps({"proposal_ref": status["proposal_ref"]}), parent, child
    )
    assert result["proposal"] == target.model_dump()
    assert not result["feedback"]
    assert (
        publication_result(target.model_dump_json(), parent, child)["proposal"]
        == target.model_dump()
    )


@pytest.mark.parametrize("stop_loss_pct", [None, 0.2])
def test_research_loadings_publish_without_an_exit_policy(
    run: tuple[list[dict], list[dict]], stop_loss_pct: float | None
) -> None:
    parent, child = run
    for index, message in enumerate(parent):
        for part in message.get("parts", []):
            if part.get("tool") != CHECKPOINT_TOOL:
                continue
            checkpoint = part["state"]["input"]["checkpoint"]
            variant = (checkpoint.get("draft") or {}).get("variant")
            if variant:
                for position in variant["positions"]:
                    position["stop_loss_pct"] = stop_loss_pct
                    position["take_profit_pct"] = None
                parent[index] = receipt(checkpoint, part["state"]["time"]["end"])
    status = draft_status(parent, child)
    assert status["ready"], status["errors"]
    result = publication_result(
        json.dumps({"proposal_ref": status["proposal_ref"]}), parent, child
    )
    assert not result["feedback"]
    for variant in result["proposal"]["variants"]:
        assert variant["cash_bps"] == 0
        assert sum(p["capital_bps"] for p in variant["positions"]) == 10000
        assert all(p["stop_loss_pct"] == stop_loss_pct for p in variant["positions"])


def test_partial_or_child_drafts_cannot_publish(run):
    parent, child = run
    assert not draft_status(parent[:3], child)["ready"]
    assert not draft_status(parent[:2], [*child, *parent[2:6]])["ready"]
    assert publication_result("I ran out of steps", parent, child)["proposal"] is None


def test_full_json_cannot_bypass_the_saved_reviewed_draft(run, target):
    parent, child = run
    changed = target.model_dump()
    changed["variants"][0]["positions"][0]["instrument_id"] = "UNREVIEWED-USDC"
    result = publication_result(json.dumps(changed), parent, child)
    assert result["proposal"] is None
    assert "differs from the saved draft" in result["feedback"]


def test_reference_is_scoped_and_invalidated_by_updates(run):
    parent, child = run
    reference = draft_status(parent, child)["proposal_ref"]
    other = deepcopy(parent)
    for message in other:
        message["info"]["sessionID"] = "different-run"
    assert (
        publication_result(json.dumps({"proposal_ref": reference}), other, child)[
            "proposal"
        ]
        is None
    )
    update = {
        "schema_version": 4,
        "stage": "draft",
        "construction": {"mode": "directional"},
        "draft": {
            "components": [
                {
                    **draft_context(parent, child)[0]["components"][0],
                    "rationale": "Changed",
                }
            ]
        },
    }
    parent.append(receipt(update, 11))
    assert (
        publication_result(json.dumps({"proposal_ref": reference}), parent, child)[
            "proposal"
        ]
        is None
    )
    assert len(draft_context(parent, child)[0]["variants"]) == 4


def test_missing_evidence_review_and_non_json_feedback_are_combined(run):
    parent, _ = run
    parent = [
        m
        for m in parent
        if not any(
            p.get("tool")
            in {"wayfinder_core_web_fetch", "wayfinder_research_quantify_portfolio"}
            for p in m["parts"]
        )
    ]
    status = draft_status(parent, [])
    assert not status["ready"]
    feedback = publication_result("Done later", parent, [])["feedback"]
    assert "Final answer" in feedback
    assert "source actually read" in feedback
    assert "quantify" in feedback
    assert "reviewer" in feedback


def test_index_only_review_does_not_pass(run):
    parent, child = run
    child[0]["parts"][0]["state"]["output"] = json.dumps(research_notebook(parent, []))
    status = draft_status(parent, child)
    assert not status["ready"]
    assert status["review"]["unread_selected"] == ["bitcoin"]


@pytest.mark.parametrize("read_case", [True, False])
def test_review_coverage_follows_observed_lookup_aliases(run, read_case):
    parent, child = run
    case = deepcopy(checkpoints(parent)[-1]["checkpoint"])
    case["candidates"][0]["instruments"] = ["bitcoin-provider-id"]
    parent.append(receipt(case, 11))
    parent.append(
        observation(
            "wayfinder_onchain_resolve_token",
            {
                "token_id": "BTC-USDC",
                "lookup_id": "bitcoin-provider-id",
                "address": "0xabc",
                "chain": {"id": 1, "code": "ethereum"},
            },
            12,
        )
    )
    if not read_case:
        child[0]["parts"][0]["state"]["output"] = json.dumps(
            research_notebook(parent, [])
        )
    status = draft_status(parent, child)
    assert status["ready"] is read_case
    assert status["review"]["unread_selected"] == ([] if read_case else ["bitcoin"])


def test_construction_cannot_drift_but_can_be_restored(run):
    parent, child = run
    base = {
        "schema_version": 4,
        "stage": "judged",
        "discovery_dispositions": [
            {
                "entities": ["bitcoin"],
                "status": "assessed",
                "candidate_entity": "bitcoin",
                "reason": "Same",
            }
        ],
    }
    parent.append(
        receipt(
            {
                **base,
                "construction": {
                    "mode": "matched_relative",
                    "benchmark": "BTC",
                    "benchmark_direction": "short",
                },
            },
            11,
        )
    )
    assert "Construction changed" in " ".join(draft_status(parent, child)["errors"])
    parent.append(receipt({**base, "construction": {"mode": "directional"}}, 12))
    assert draft_status(parent, child)["ready"]


def test_late_ranked_alias_must_be_explicitly_reconciled(run, spec):
    parent, child = run
    case = checkpoints(parent)[-1]["checkpoint"]["candidates"][0]
    research = {
        k: v
        for k, v in case.items()
        if k not in {"decision", "decision_basis", "reason", "implementation_checks"}
    }
    research["entity"] = "bitcoin-btc"
    child.append(
        receipt(
            {
                "schema_version": 3,
                "stage": "discovery",
                "spec": spec,
                "research_cases": [research],
            },
            11,
            "worker",
            "thesis-researcher",
        )
    )
    status = draft_status(parent, child)
    assert status["assessment"]["missing_entities"] == ["bitcoin-btc"]
    parent.append(
        receipt(
            {
                "schema_version": 4,
                "stage": "judged",
                "construction": {"mode": "directional"},
                "discovery_dispositions": [
                    {
                        "entities": ["bitcoin-btc"],
                        "status": "assessed",
                        "candidate_entity": "bitcoin",
                        "reason": "Same observed BTC underlying",
                    }
                ],
            },
            12,
        )
    )
    assert draft_status(parent, child)["ready"]


def relative_variant(target, long_leverage=1, short_leverage=1, budget=1000):
    first = target.variants[0].positions[0].model_dump()
    return Variant.model_validate(
        {
            "budget_usd": budget,
            "cash_bps": 0,
            "rationale": "Fixture",
            "positions": [
                {
                    **first,
                    "id": "thesis",
                    "instrument_id": "AI-USDC",
                    "capital_bps": 7000,
                    "leverage": long_leverage,
                },
                {
                    **first,
                    "id": "benchmark",
                    "direction": "short",
                    "capital_bps": 3000,
                    "leverage": short_leverage,
                },
            ],
        }
    )


@pytest.fixture
def relative():
    return Construction(
        mode="matched_relative",
        benchmark="Bitcoin",
        benchmark_direction="short",
        benchmark_instrument_id="BTC-USDC",
    )


@pytest.mark.parametrize("budget", [100, 1000, 10000, 100000])
@pytest.mark.parametrize("leverages", [(1, 1), (1, 2), (2, 1), (1.3, 1.7), (2, 2)])
def test_relative_sizing_preserves_legs_leverage_and_full_capital(
    target, relative, budget, leverages
):
    original = relative_variant(target, *leverages, budget=budget)
    sized = size_variant(original, relative)
    assert not construction_errors(sized, relative)
    assert sum(p.capital_bps for p in sized.positions) == 10000
    assert sized.cash_bps == 0
    assert [(p.instrument_id, p.leverage) for p in sized.positions] == [
        (p.instrument_id, p.leverage) for p in original.positions
    ]
    if leverages == (1, 1):
        assert [p.capital_bps for p in sized.positions] == [5000, 5000]


@pytest.mark.parametrize(
    "a,b,expected",
    [(0.7, 0.4, 0.15), (-0.1, -0.2, 0.05), (0.2, 0.4, -0.1), (0.4, 0.4, 0)],
)
def test_spread_payoff_not_standalone_benchmark_return(
    target, relative, a, b, expected
):
    sized = size_variant(relative_variant(target), relative)
    metrics = quantify_variants(
        [sized],
        {
            "AI-USDC": {"prices": {0: 100, DAY_MS: 100 * (1 + a)}},
            "BTC-USDC": {"prices": {0: 100, DAY_MS: 100 * (1 + b)}},
        },
    )
    assert metrics["portfolios"][0]["metrics"]["price_return"] == pytest.approx(
        expected
    )


def test_missing_benchmark_and_infeasible_minimums_are_not_silently_repaired(
    target, relative
):
    long_only = target.variants[0]
    with pytest.raises(ValueError, match="benchmark"):
        size_variant(long_only, relative)
    v = relative_variant(target, budget=100).model_dump()
    v["positions"][0]["capital_bps"] = 1
    v["positions"].append(
        {
            **v["positions"][0],
            "id": "other",
            "instrument_id": "OTHER-USDC",
            "capital_bps": 6999,
        }
    )
    with pytest.raises(ValueError, match="minimums"):
        size_variant(Variant.model_validate(v), relative)


def test_v3_receipts_still_match_pre_incremental_bytes(spec):
    cp = ResearchCheckpoint(schema_version=3, stage="interpretation", spec=spec)
    old = cp.model_dump(exclude={"construction", "draft"})
    message = receipt(old, 1)
    message["parts"][0]["state"]["input"]["checkpoint"] = old
    assert len(checkpoints([message])) == 1
