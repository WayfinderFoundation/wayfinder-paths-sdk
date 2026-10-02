import hashlib
import json
from copy import deepcopy
from typing import Any

import pytest

from wayfinder_paths.core.theses.assessment import (
    checkpoints,
    projected_records,
    research_notebook,
)
from wayfinder_paths.core.theses.checkpoints import ResearchCheckpoint
from wayfinder_paths.core.theses.draft import draft_status
from wayfinder_paths.core.theses.review import decision_evidence, public_observations
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
