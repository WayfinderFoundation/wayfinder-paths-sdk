import hashlib
import json
from copy import deepcopy
from typing import Any
from unittest.mock import patch

import pytest
from mcp.server.fastmcp.tools.base import Tool

from wayfinder_paths.core.theses.assessment import (
    checkpoints,
    projected_records,
    research_notebook,
)
from wayfinder_paths.core.theses.checkpoints import ResearchCheckpoint
from wayfinder_paths.core.theses.draft import draft_status
from wayfinder_paths.core.theses.review import decision_evidence, public_observations
from wayfinder_paths.mcp.tools.thesis_checkpoint import research_thesis_checkpoint
from wayfinder_paths.tests import test_thesis_handoffs
from wayfinder_paths.tests.test_thesis_draft import observation, receipt

case = test_thesis_handoffs.case
discovery = test_thesis_handoffs.discovery
spec = test_thesis_handoffs.spec
compact_run = test_thesis_handoffs.compact_run
Run = tuple[list[dict[str, Any]], list[dict[str, Any]]]


@pytest.fixture
def decision_run(compact_run: Run) -> Run:
    parent, children = compact_run
    cp = parent[0]["parts"][0]["state"]["input"]["checkpoint"]
    cp["schema_version"] = 7
    cp["decisions"][0]["claims"] = [
        {
            "statement": "Growing usage may increase demand for the instrument",
            "basis": "inference",
            "scope": "Observed activity, not guaranteed holder revenue or an expected return",
            "evidence_part_ids": ["proof"],
        }
    ]
    proof = observation(
        "wayfinder_core_web_fetch", {"results": [{"text": "Usage increased"}]}, 1
    )
    proof["parts"][0]["id"] = "proof"
    return [proof, receipt(cp, 2)], children


def report(parent: list[dict], children: list[dict]) -> dict[str, Any]:
    projected, research, errors = projected_records(parent, children)
    assert not errors
    return decision_evidence(
        projected, research, public_observations([*parent, *children])
    )


def test_claim_provenance_and_inference_survive_projection(decision_run: Run) -> None:
    parent, children = decision_run
    original = deepcopy((parent, children))
    result = report(parent, children)
    assert not result["errors"]
    assert result["claim_count"] == 1
    assert result["claims"][0]["basis"] == "inference"
    assert result["claims"][0]["evidence_part_ids"] == ["proof"]
    notebook = research_notebook(
        parent, children, entities=["network"], fields=["reason"], history=False
    )
    assert notebook["cases"][0]["records"][0]["case"]["claims"]
    assert not notebook["evidence_verified"]
    assert (parent, children) == original


def test_research_correction_survives_later_decision_only_update(
    decision_run: Run,
) -> None:
    parent, children = decision_run
    cp = deepcopy(parent[1]["parts"][0]["state"]["input"]["checkpoint"])
    research = children[0]["parts"][0]["state"]["input"]["checkpoint"][
        "research_cases"
    ][0]
    cp["decisions"][0]["updated_research"] = {
        **research,
        "support": "Corrected observation, unsupported figures removed",
        "gaps": ["Remaining forward assumption"],
        "case_basis": "mixed",
    }
    parent.append(receipt(cp, 3))
    cp["decisions"][0].update(
        updated_research=None, reason="Reconsidered opportunity cost, same evidence"
    )
    parent.append(receipt(cp, 4))
    original = deepcopy((parent, children))
    projected, saved, errors = projected_records(parent, children)
    assert not errors
    current = projected[-1]["checkpoint"]["candidates"][0]
    assert current["support"] == "Corrected observation, unsupported figures removed"
    assert current["gaps"] == ["Remaining forward assumption"]
    assert current["case_basis"] == "mixed"
    assert current["reason"] == "Reconsidered opportunity cost, same evidence"
    assert saved[0]["checkpoint"]["research_cases"][0]["support"] == research["support"]
    notebook = research_notebook(
        parent, children, entities=["network"], fields=["support"], history=False
    )
    assert notebook["cases"][0]["records"][0]["case"]["support"] == current["support"]
    assert (parent, children) == original


def test_partial_research_corrections_merge_without_resurrecting_stale_fields(
    decision_run: Run,
) -> None:
    parent, children = decision_run
    original_children = deepcopy(children)
    cp = deepcopy(parent[1]["parts"][0]["state"]["input"]["checkpoint"])
    correction = {
        "observed_identifiers": ["Observed provider slug, not an absent listing"],
        "value_capture": "Demand-linked burn, not a holder cash distribution",
        "counterevidence": "Declining measured usage remains a risk",
        "gaps": [],
    }
    cp["decisions"][0]["updated_research"] = correction
    parent.append(receipt(cp, 3))
    cp["decisions"][0]["updated_research"] = {
        "closest_alternative": "Current supported competitor; old claim withdrawn"
    }
    parent.append(receipt(cp, 4))
    cp["decisions"][0]["updated_research"] = None
    parent.append(receipt(cp, 5))
    records, _, errors = projected_records(parent, children)
    assert not errors
    current = records[-1]["checkpoint"]["candidates"][0]
    assert {field: current[field] for field in correction} == correction
    assert current["closest_alternative"].startswith("Current supported competitor")
    source = children[0]["parts"][0]["state"]["input"]["checkpoint"]["research_cases"][
        0
    ]
    assert current["support"] == source["support"]
    assert current["instruments"] == source["instruments"]
    assert children == original_children
    assert not report(parent, children)["errors"]


@pytest.mark.parametrize(
    "correction",
    [
        {},
        {"support": None},
        {"support": ""},
        {"value_capture": ["wrong type"]},
        {"gaps": ["gap"] * 7},
        {"observed_identifiers": []},
        {"decision": "KEEP"},
        {"support": "Changed", "unrecognized": "Cannot silently ignore"},
    ],
)
def test_partial_research_correction_rejects_invalid_fields(
    decision_run: Run, correction: dict[str, Any]
) -> None:
    parent, _ = decision_run
    cp = deepcopy(parent[1]["parts"][0]["state"]["input"]["checkpoint"])
    cp["decisions"][0]["updated_research"] = correction
    with pytest.raises(ValueError):
        ResearchCheckpoint.model_validate(cp)


def test_partial_research_correction_is_receipt_bound(decision_run: Run) -> None:
    parent, children = decision_run
    cp = deepcopy(parent[1]["parts"][0]["state"]["input"]["checkpoint"])
    cp["decisions"][0]["updated_research"] = {"support": "Corrected observation"}
    message = receipt(cp, 3)
    assert len(checkpoints([message])) == 1
    message["parts"][0]["state"]["input"]["checkpoint"]["decisions"][0][
        "updated_research"
    ]["value_capture"] = "An unsigned correction"
    assert not checkpoints([message])
    assert not draft_status([*parent, message], children)["ready"]


@pytest.mark.asyncio
async def test_partial_research_correction_mcp_receipt_replays(
    decision_run: Run,
) -> None:
    parent, children = decision_run
    cp = deepcopy(parent[1]["parts"][0]["state"]["input"]["checkpoint"])
    cp["decisions"][0]["updated_research"] = {"closest_alternative": "Corrected rival"}
    native = Tool.from_function(research_thesis_checkpoint)
    with patch("wayfinder_paths.mcp.utils._report_tool_metric"):
        result = await native.run({"checkpoint": cp})
    assert result["ok"] is True
    assert result["result"]["evidence_verified"] is False
    assert result["result"]["execution_authorized"] is False
    message = receipt(cp, 3)
    message["parts"][0]["state"]["input"]["checkpoint"] = cp
    message["parts"][0]["state"]["output"] = json.dumps(result)
    records, _, errors = projected_records([*parent, message], children)
    assert not errors
    assert records[-1]["checkpoint"]["candidates"][0]["closest_alternative"] == (
        "Corrected rival"
    )


@pytest.mark.parametrize(
    "change",
    ["decision_only", "partial_correction", "full_replacement", "source", "entity"],
)
def test_decision_only_update_preserves_implementation_and_comparison_evidence(
    decision_run: Run, change: str
) -> None:
    parent, children = decision_run
    cp = deepcopy(parent[1]["parts"][0]["state"]["input"]["checkpoint"])
    decision = cp["decisions"][0]
    checks = [
        {
            "kind": "spot",
            "instrument_id": "network-solana",
            "status": "viable",
            "reason": "Observed alternate implementation",
            "observations": ["Saved chain-scoped lookup"],
        }
    ]
    worker = deepcopy(children[0]["parts"][0]["state"]["input"]["checkpoint"])
    worker["research_cases"][0]["entity"] = "rival"
    children.append(
        receipt(worker, 1, session="other-worker", agent="thesis-researcher")
    )
    refs = [{"session_id": "other-worker", "checkpoint_id": "t1", "entity": "rival"}]
    decision.update(implementation_checks=checks, comparison_refs=refs)
    parent.append(receipt(cp, 3))
    decision.update(
        reason="Corrected rationale without changing the implementation comparison",
        implementation_checks=[],
        comparison_refs=[],
    )
    if change == "full_replacement":
        decision["updated_research"] = deepcopy(
            children[0]["parts"][0]["state"]["input"]["checkpoint"]["research_cases"][0]
        )
    elif change == "partial_correction":
        decision["updated_research"] = {"closest_alternative": "Corrected comparison"}
    elif change == "source":
        decision["research_ref"] = dict(refs[0])
    elif change == "entity":
        decision["entity"] = "different-underlying"
    parent.append(receipt(cp, 4))
    original = deepcopy((parent, children))
    projected, _, errors = projected_records(parent, children)
    assert not errors
    current = projected[-1]["checkpoint"]["candidates"][0]
    assert current["implementation_checks"] == (
        checks if change in {"decision_only", "partial_correction"} else []
    )
    assert current["comparison_refs"] == (
        refs if change in {"decision_only", "partial_correction"} else []
    )
    assert current["reason"] == decision["reason"]
    assert (parent, children) == original


@pytest.mark.parametrize("change", ["source", "entity", "invalid_update"])
@pytest.mark.parametrize("partial", [False, True])
def test_research_correction_is_scoped_and_only_valid_updates_replace_it(
    decision_run: Run, change: str, partial: bool
) -> None:
    parent, children = decision_run
    cp = deepcopy(parent[1]["parts"][0]["state"]["input"]["checkpoint"])
    worker = deepcopy(children[0]["parts"][0]["state"]["input"]["checkpoint"])
    research = worker["research_cases"][0]
    cp["decisions"][0]["updated_research"] = {
        **({} if partial else research),
        "support": "Correction",
    }
    parent.append(receipt(cp, 3))
    cp["decisions"][0]["updated_research"] = None
    if change == "source":
        worker["research_cases"][0]["support"] = "Different saved research"
        children.append(
            receipt(worker, 1, session="other-worker", agent="thesis-researcher")
        )
        cp["decisions"][0]["research_ref"]["session_id"] = "other-worker"
        expected = "Different saved research"
    elif change == "entity":
        cp["decisions"][0]["entity"] = "another-entity"
        expected = research["support"]
    else:
        invalid = deepcopy(cp)
        invalid["decisions"][0].update(
            updated_research={
                **({} if partial else research),
                "support": "Must not replace correction",
            },
            decision="REJECT",
            decision_basis="implementation",
            implementation_checks=[],
        )
        parent.append(receipt(invalid, 4))
        assert projected_records(parent, children)[2]
        expected = "Correction"
    parent.append(receipt(cp, 5))
    projected, _, errors = projected_records(parent, children)
    assert not errors
    assert projected[-1]["checkpoint"]["candidates"][0]["support"] == expected


@pytest.mark.parametrize(
    "invalid", ["missing", "failed", "private", "future", "empty", "search_snippet"]
)
def test_claim_cannot_launder_unusable_evidence(
    decision_run: Run, invalid: str
) -> None:
    parent, children = decision_run
    part = parent[0]["parts"][0]
    if invalid == "missing":
        part["id"] = "different"
    elif invalid == "failed":
        part["state"]["output"] = json.dumps(
            {"ok": False, "result": {"error": "provider failed"}}
        )
    elif invalid == "private":
        part["tool"] = "wayfinder_core_get_wallets"
    elif invalid == "future":
        part["state"]["time"]["end"] = 3
    elif invalid == "search_snippet":
        part["tool"] = "wayfinder_core_web_search"
    else:
        part["state"]["output"] = json.dumps({"ok": True, "result": {}})
    result = report(parent, children)
    assert "earlier successful public reads" in result["errors"][0]
    assert result["claims"][0]["unavailable_part_ids"] == ["proof"]


def test_modern_decision_needs_claim_but_unresolved_is_not_rejection(
    decision_run: Run,
) -> None:
    parent, _ = decision_run
    cp = parent[1]["parts"][0]["state"]["input"]["checkpoint"]
    decision = cp["decisions"][0]
    decision["claims"] = []
    with pytest.raises(ValueError, match="attach 1-4 decisive claims"):
        ResearchCheckpoint.model_validate(cp)
    decision.update(decision="NEEDS_EVIDENCE", decision_basis="unresolved")
    assert ResearchCheckpoint.model_validate(cp).decisions[0].claims == []


def test_legacy_overwrite_cannot_remove_claim_requirement(decision_run: Run) -> None:
    parent, children = decision_run
    old = deepcopy(parent[1]["parts"][0]["state"]["input"]["checkpoint"])
    old["schema_version"] = 6
    old["decisions"][0]["claims"] = []
    parent.append(receipt(old, 3))
    assert "requires source-linked claims" in report(parent, children)["errors"][0]


def test_draft_indexes_claims_and_status_stays_compact(decision_run: Run) -> None:
    parent, children = decision_run
    draft = draft_status(parent, children, include_proposal=True)
    status = draft_status(parent, children)
    assert draft["review"]["decision_evidence"]["claim_index"] == [
        {"entity": "network", "checkpoint_id": "t2", "evidence_part_ids": ["proof"]}
    ]
    assert "claims" not in draft["review"]["decision_evidence"]
    assert status["review"]["decision_evidence"]["claim_count"] == 1
    assert "claims" not in status["review"]["decision_evidence"]
    parent[0]["parts"][0]["tool"] = "private"
    invalid = draft_status(parent, children)
    assert any(
        "earlier successful public reads" in error for error in invalid["errors"]
    )
    assert not invalid["ready"]


def test_long_claims_remain_retrievable_without_overflowing_draft(
    decision_run: Run,
) -> None:
    parent, children = decision_run
    projected, _, _ = projected_records(parent, children)
    case = deepcopy(projected[0]["checkpoint"]["candidates"][0])
    long_claim = {**case["claims"][0], "statement": "s" * 2000, "scope": "p" * 2000}
    cases = [
        {**case, "entity": f"case-{i}", "claims": [long_claim] * 4} for i in range(8)
    ]
    parent.append(
        receipt({"schema_version": 7, "stage": "judged", "candidates": cases}, 3)
    )
    assert len(json.dumps(report(parent, children)["claims"])) > 128_000
    seen: list[str] = []
    offset = 0
    while offset is not None:
        draft = draft_status(
            parent, children, include_proposal=True, limit=2, offset=offset
        )
        assert len(json.dumps(draft).encode()) < 48_000
        evidence = draft["review"]["decision_evidence"]
        assert len(evidence["claim_index"]) <= 2
        seen.extend(row["entity"] for row in evidence["claim_index"])
        offset = evidence["claim_index_page"]["next_offset"]
    assert seen == [*[f"case-{i}" for i in range(8)], "network"]
    row = research_notebook(
        parent, children, entities=["case-0"], fields=["reason"], history=False
    )
    assert row["cases"][0]["records"][0]["case"]["claims"] == [long_claim] * 4


@pytest.mark.parametrize("version", [5, 6])
def test_legacy_decision_receipt_bytes_and_reader_preserved(
    compact_run: Run, version: int
) -> None:
    parent, _ = compact_run
    raw = parent[0]["parts"][0]["state"]["input"]["checkpoint"]
    raw["schema_version"] = version
    for decision in raw["decisions"]:
        decision.pop("claims", None)
        decision.pop("comparison_refs", None)
    old = json.dumps(raw, separators=(",", ":"))
    cp = ResearchCheckpoint.model_validate(raw)
    assert cp.receipt_json() == old
    parent[0]["parts"][0]["state"]["output"] = json.dumps(
        {"ok": True, "result": {"sha256": hashlib.sha256(old.encode()).hexdigest()}}
    )
    assert checkpoints(parent)[0]["checkpoint"]["schema_version"] == version


def test_comparison_tracks_changed_input_without_inventing_new_verdict(
    decision_run: Run,
) -> None:
    parent, children = decision_run
    worker = deepcopy(children[0]["parts"][0]["state"]["input"]["checkpoint"])
    rival = worker["research_cases"][0]
    rival["entity"] = "rival"
    children.append(
        receipt(worker, 1, session="other-worker", agent="thesis-researcher")
    )
    cp = parent[1]["parts"][0]["state"]["input"]["checkpoint"]
    ref = {"session_id": "other-worker", "checkpoint_id": "t1", "entity": "rival"}
    cp["decisions"][0]["comparison_refs"] = [ref]
    parent[1] = receipt(cp, 2)
    assert report(parent, children)["comparison_updates"] == []
    rival_decision = deepcopy(cp["decisions"][0])
    rival_decision.update(
        entity="rival",
        research_ref=ref,
        comparison_refs=[],
        updated_research={
            **rival,
            "support": "Corrected period gives a lower growth rate",
        },
    )
    parent.append(
        receipt(
            {"schema_version": 7, "stage": "judged", "decisions": [rival_decision]}, 5
        )
    )
    result = report(parent, children)
    assert not result["errors"]
    update = result["comparison_updates"][0]
    assert update["entity"] == "network"
    assert update["compared_entity"] == "rival"
    assert update["changed_fields"] == ["support"]
    cp["decisions"][0]["comparison_refs"] = [update["current_ref"]]
    parent.append(receipt(cp, 6))
    assert report(parent, children)["comparison_updates"] == []


@pytest.mark.parametrize("bad_ref", ["unknown", "future", "self"])
def test_comparison_requires_real_earlier_other_case(
    decision_run: Run, bad_ref: str
) -> None:
    parent, children = decision_run
    cp = parent[1]["parts"][0]["state"]["input"]["checkpoint"]
    ref = {"session_id": "other-worker", "checkpoint_id": "t3", "entity": "rival"}
    if bad_ref == "self":
        ref["entity"] = "network"
    elif bad_ref == "future":
        worker = deepcopy(children[0]["parts"][0]["state"]["input"]["checkpoint"])
        worker["research_cases"][0]["entity"] = "rival"
        children.append(
            receipt(worker, 3, session="other-worker", agent="thesis-researcher")
        )
    cp["decisions"][0]["comparison_refs"] = [ref]
    if bad_ref == "self":
        with pytest.raises(ValueError, match="another entity"):
            ResearchCheckpoint.model_validate(cp)
    else:
        parent[1] = receipt(cp, 2)
        assert "not an earlier saved case" in report(parent, children)["errors"][0]


@pytest.mark.parametrize(
    "field", ["decision", "reason", "claims", "implementation_checks"]
)
@pytest.mark.parametrize("judged_first", [True, False])
def test_worker_comparison_tracks_later_parent_decision_changes(
    decision_run: Run, field: str, judged_first: bool
) -> None:
    parent, children = decision_run
    worker = deepcopy(children[0]["parts"][0]["state"]["input"]["checkpoint"])
    worker["research_cases"][0]["entity"] = "rival"
    children.append(
        receipt(worker, 1, session="other-worker", agent="thesis-researcher")
    )
    ref = {"session_id": "other-worker", "checkpoint_id": "t1", "entity": "rival"}
    dependent = deepcopy(parent[1]["parts"][0]["state"]["input"]["checkpoint"])
    dependent["decisions"][0]["comparison_refs"] = [ref]
    rival = deepcopy(dependent["decisions"][0])
    rival.update(entity="rival", research_ref=ref, comparison_refs=[])
    judged = {"schema_version": 7, "stage": "judged", "decisions": [rival]}
    parent = [
        parent[0],
        receipt(judged if judged_first else dependent, 2),
        receipt(dependent if judged_first else judged, 3),
    ]
    assert report(parent, children)["comparison_updates"] == []

    if field == "decision":
        rival["decision"] = "REJECT"
    elif field == "reason":
        rival["reason"] = "The original preference no longer holds"
    elif field == "claims":
        rival["claims"][0]["statement"] = "Corrected interpretation of observed usage"
    else:
        rival["implementation_checks"] = [
            {
                "kind": "spot",
                "instrument_id": None,
                "status": "unverified",
                "reason": "Reconsidered implementation",
                "observations": [],
            }
        ]
    parent.append(receipt(judged, 4))
    original = deepcopy((parent, children))
    result = report(parent, children)
    assert not result["errors"]
    assert len(result["comparison_updates"]) == 1
    update = result["comparison_updates"][0]
    assert update["entity"] == "network"
    assert update["compared_entity"] == "rival"
    assert update["compared_ref"] == ref
    assert update["changed_fields"] == [field]
    assert update["current_ref"]["checkpoint_id"] == "t4"
    assert (parent, children) == original

    # Refreshing the dependent assessment marks reconsideration, not approval.
    parent.append(receipt(dependent, 5))
    assert report(parent, children)["comparison_updates"] == []
    rival["reason"] = "Another material change after reconsideration"
    parent.append(receipt(judged, 6))
    assert report(parent, children)["comparison_updates"][0]["changed_fields"] == [
        "reason"
    ]
