import hashlib
import json
from collections.abc import Sequence
from typing import Any

import pytest
from pydantic import ValidationError

from wayfinder_paths.core.theses.assessment import (
    CHECKPOINT_TOOL,
    assessment_report,
    checkpoints,
)
from wayfinder_paths.core.theses.checkpoints import ResearchCheckpoint


@pytest.fixture
def spec() -> dict[str, Any]:
    return {
        "objective": "Infrastructure revenue growth",
        "horizon": "6 months",
        "constraints": [],
        "mechanisms": ["Data demand"],
        "causal_chain": "Demand to revenue to token value",
        "counterfactual": "Dilution exceeds growth",
        "baseline": "Direct exposure",
    }


@pytest.fixture
def discovery() -> dict[str, Any]:
    return {
        "entity": "network",
        "name": "Network",
        "mechanism": "Data demand",
        "observed_identifiers": ["network-id"],
        "sources": ["https://example.test"],
        "instruments": ["NETWORK-USDC", "network-solana"],
    }


@pytest.fixture
def case(discovery: dict[str, Any]) -> dict[str, Any]:
    return dict(
        **discovery,
        effect_order=2,
        value_capture="Fees",
        support="Growing usage",
        counterevidence="Dilution",
        closest_alternative="Other network",
        decision="ALTERNATIVE",
        decision_basis="implementation",
        reason="Carry cost",
        gaps=[],
        implementation_checks=[
            {
                "kind": "perp",
                "instrument_id": "NETWORK-USDC",
                "status": "rejected",
                "reason": "Carry cost",
                "observations": ["NETWORK-USDC"],
            },
            {
                "kind": "spot",
                "instrument_id": "network-solana",
                "status": "rejected",
                "reason": "Observed wrapper risk exceeds direct alternative",
                "observations": ["network-solana"],
            },
        ],
    )


def record(
    spec: dict[str, Any],
    stage: str,
    *,
    discoveries: Sequence[dict[str, Any]] = (),
    candidates: Sequence[dict[str, Any]] = (),
    session: str | None = None,
) -> dict[str, Any]:
    checkpoint = ResearchCheckpoint.model_validate(
        {
            "schema_version": 2,
            "stage": stage,
            "spec": spec,
            "discoveries": list(discoveries),
            "candidates": list(candidates),
        }
    )
    return {
        "info": {"agent": "thesis-researcher", "sessionID": session} if session else {},
        "parts": [
            {
                "tool": CHECKPOINT_TOOL,
                "state": {
                    "status": "completed",
                    "input": {"checkpoint": checkpoint.model_dump()},
                    "output": json.dumps(
                        {
                            "ok": True,
                            "result": {
                                "sha256": hashlib.sha256(
                                    checkpoint.model_dump_json().encode()
                                ).hexdigest(),
                            },
                        }
                    ),
                },
            }
        ],
    }


def observed() -> dict[str, Any]:
    return {
        "parts": [
            {
                "tool": "wayfinder_onchain_fuzzy_search_tokens",
                "state": {
                    "status": "completed",
                    "output": json.dumps(
                        {
                            "ok": True,
                            "result": {
                                "ids": ["NETWORK-USDC", "network-solana"],
                            },
                        }
                    ),
                },
            }
        ]
    }


def test_worker_discovery_cannot_disappear_from_later_shortlist(
    spec: dict[str, Any],
    discovery: dict[str, Any],
    case: dict[str, Any],
) -> None:
    child = record(spec, "discovery", discoveries=[discovery], session="worker")
    other = dict(case, entity="other", observed_identifiers=["other"], instruments=[])
    parent = record(spec, "judged", candidates=[other])
    report = assessment_report([parent, observed()], [child])
    assert report["missing_entities"] == ["network"]
    assert "Discoveries missing assessment" in " ".join(report["errors"])


def test_all_snapshots_union_and_entity_aliases_merge(
    spec: dict[str, Any],
    discovery: dict[str, Any],
    case: dict[str, Any],
) -> None:
    child = record(spec, "discovery", discoveries=[discovery], session="worker")
    empty = record(spec, "discovery", session="worker")
    renamed = dict(case, entity="canonical-network")
    parent = record(spec, "judged", candidates=[renamed])
    assert not assessment_report([parent, observed()], [child, empty])["errors"]
    # Losing an implementation in a later ledger is not allowed either.
    renamed["instruments"] = ["NETWORK-USDC"]
    parent = record(spec, "judged", candidates=[renamed])
    assert (
        "Known implementation IDs lost: network-solana"
        in assessment_report([parent, observed()], [child])["errors"]
    )


def test_perp_cost_cannot_exclude_spot_without_comparison(
    spec: dict[str, Any], case: dict[str, Any]
) -> None:
    case["implementation_checks"] = case["implementation_checks"][:1]
    with pytest.raises(ValidationError, match="compare alternative implementations"):
        record(spec, "judged", candidates=[case])


def test_viable_spot_prevents_implementation_only_exclusion(
    spec: dict[str, Any], case: dict[str, Any]
) -> None:
    case["implementation_checks"][1]["status"] = "viable"
    with pytest.raises(ValidationError, match="viable alternative remains"):
        record(spec, "judged", candidates=[case])


def test_comparing_different_alternative_cannot_hide_known_spot(
    spec: dict[str, Any],
    discovery: dict[str, Any],
    case: dict[str, Any],
) -> None:
    case["implementation_checks"][1]["instrument_id"] = "another-spot"
    parent = record(spec, "judged", candidates=[case])
    child = record(spec, "discovery", discoveries=[discovery])
    errors = assessment_report([parent, observed()], [child])["errors"]
    assert any("did not compare network-solana" in e for e in errors)


def test_failure_is_unresolved_not_negative_evidence(
    spec: dict[str, Any], case: dict[str, Any]
) -> None:
    case["implementation_checks"][1]["status"] = "unverified"
    with pytest.raises(ValidationError, match="NEEDS_EVIDENCE"):
        record(spec, "judged", candidates=[case])
    case.update(decision="NEEDS_EVIDENCE", decision_basis="unresolved")
    parent = record(spec, "judged", candidates=[case])
    report = assessment_report([parent, observed()], [])
    assert not report["errors"]
    assert report["unresolved_entities"] == ["network"]


def test_economic_rejection_does_not_force_unnecessary_venue_hydration(
    spec: dict[str, Any], case: dict[str, Any]
) -> None:
    case.update(
        decision="REJECT",
        decision_basis="economic",
        implementation_checks=[],
        reason="No supported link from demand to token value",
    )
    parent = record(spec, "judged", candidates=[case])
    assert not assessment_report([parent], [])["errors"]


def test_short_does_not_require_spot_trade(
    spec: dict[str, Any], case: dict[str, Any]
) -> None:
    case["implementation_checks"][1].update(
        status="incompatible", reason="Spot cannot short"
    )
    parent = record(spec, "judged", candidates=[case])
    assert not assessment_report([parent, observed()], [])["errors"]


def test_checkpoint_claims_are_not_implementation_observations(
    spec: dict[str, Any], case: dict[str, Any]
) -> None:
    parent = record(spec, "judged", candidates=[case])
    errors = assessment_report([parent], [])["errors"]
    assert any("references were not observed" in e for e in errors)


def test_empty_judgment_cannot_erase_full_ledger(spec: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="retain the assessment ledger"):
        record(spec, "judged")


def test_children_cannot_judge_and_must_capture_inventory(
    spec: dict[str, Any], case: dict[str, Any]
) -> None:
    child = record(spec, "judged", candidates=[case], session="worker")
    errors = assessment_report([observed()], [child])["errors"]
    assert "Researchers may record discoveries, not parent judgments" in errors
    assert any("did not record its discovery inventory" in e for e in errors)


def test_forged_receipt_does_not_admit_discoveries(
    spec: dict[str, Any], discovery: dict[str, Any]
) -> None:
    child = record(spec, "discovery", discoveries=[discovery])
    child["parts"][0]["state"]["output"] = '{"ok":true,"result":{"sha256":"fake"}}'
    assert not checkpoints([child])


def test_invented_id_labels_do_not_become_mandatory_implementations(
    spec: dict[str, Any],
    discovery: dict[str, Any],
    case: dict[str, Any],
) -> None:
    discovery["instruments"] = ["hl:NETWORK-USDC", "network-solana"]
    child = record(spec, "discovery", discoveries=[discovery])
    parent = record(spec, "judged", candidates=[case])
    report = assessment_report([parent, observed()], [child])
    assert not report["errors"]
    assert report["unobserved_instrument_claims"] == ["hl:NETWORK-USDC"]
