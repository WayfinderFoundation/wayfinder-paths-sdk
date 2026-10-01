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
    dispositions: Sequence[dict[str, Any]] = (),
    session: str | None = None,
    schema_version: int = 2,
) -> dict[str, Any]:
    checkpoint = ResearchCheckpoint.model_validate(
        {
            "schema_version": schema_version,
            "stage": stage,
            "spec": spec,
            "discoveries": list(discoveries),
            "candidates": list(candidates),
            "discovery_dispositions": list(dispositions),
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
    links = [
        {
            "entities": ["network"],
            "status": "assessed",
            "candidate_entity": "canonical-network",
            "reason": "Same observed underlying",
        }
    ]
    parent = record(spec, "judged", candidates=[renamed], dispositions=links)
    assert not assessment_report([parent, observed()], [child, empty])["errors"]
    # The inventory owns known IDs; the case need not copy every implementation.
    renamed["instruments"] = ["NETWORK-USDC"]
    parent = record(spec, "judged", candidates=[renamed], dispositions=links)
    assert not assessment_report([parent, observed()], [child])["errors"]


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
    assert any("ID was not observed" in e for e in errors)


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


@pytest.mark.parametrize("status", ["out_of_scope", "needs_evidence"])
def test_unranked_leads_need_explicit_compact_dispositions_not_full_essays(
    spec: dict[str, Any],
    discovery: dict[str, Any],
    case: dict[str, Any],
    status: str,
) -> None:
    lead = {**discovery, "entity": "unranked", "observed_identifiers": ["unranked"]}
    child = record(spec, "discovery", discoveries=[lead])
    parent = record(
        spec,
        "judged",
        candidates=[case],
        dispositions=[
            {
                "entities": ["unranked"],
                "status": status,
                "reason": "Explicit reason",
            }
        ],
    )
    report = assessment_report([parent, observed()], [child])
    assert not report["errors"]
    key = (
        "deferred_discoveries"
        if status == "needs_evidence"
        else "out_of_scope_discoveries"
    )
    assert report[key] == ["unranked"]


def test_discovery_link_cannot_point_to_a_nonexistent_assessment(
    spec: dict[str, Any],
    discovery: dict[str, Any],
    case: dict[str, Any],
) -> None:
    parent = record(
        spec,
        "judged",
        candidates=[case],
        dispositions=[
            {
                "entities": ["alias"],
                "status": "assessed",
                "candidate_entity": "missing",
                "reason": "Alias",
            }
        ],
    )
    assert any(
        "missing candidate" in e
        for e in assessment_report([parent, observed()], [])["errors"]
    )


def test_duplicate_dispositions_fail(
    spec: dict[str, Any], case: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError, match="one disposition"):
        record(
            spec,
            "judged",
            candidates=[case],
            dispositions=[
                {
                    "entities": ["a", "a"],
                    "status": "needs_evidence",
                    "reason": "Uninvestigated",
                }
            ],
        )


def test_v2_receipts_before_dispositions_remain_readable(spec: dict[str, Any]) -> None:
    message = record(spec, "discovery")
    state = message["parts"][0]["state"]
    checkpoint = ResearchCheckpoint.model_validate(state["input"]["checkpoint"])
    del state["input"]["checkpoint"]["discovery_dispositions"]
    state["output"] = json.dumps(
        {
            "ok": True,
            "result": {
                "sha256": hashlib.sha256(
                    checkpoint.model_dump_json(
                        exclude={"discovery_dispositions"}
                    ).encode()
                ).hexdigest(),
            },
        }
    )
    assert len(checkpoints([message])) == 1


def test_valid_inventory_is_not_missing_when_worker_omits_version_tag(
    spec: dict[str, Any],
    discovery: dict[str, Any],
    case: dict[str, Any],
) -> None:
    child = record(
        spec, "discovery", discoveries=[discovery], session="worker", schema_version=1
    )
    parent = record(spec, "judged", candidates=[case])
    assert not assessment_report([parent, observed()], [child])["errors"]


def test_implementation_reason_does_not_require_duplicate_observation_prose(
    spec: dict[str, Any],
    case: dict[str, Any],
) -> None:
    for check in case["implementation_checks"]:
        del check["observations"]
    parent = record(spec, "judged", candidates=[case])
    assert not assessment_report([parent, observed()], [])["errors"]
