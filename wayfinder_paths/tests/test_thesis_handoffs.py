import hashlib
import json
from copy import deepcopy

import pytest

from wayfinder_paths.core.theses.assessment import (
    assessment_report,
    projected_records,
    research_notebook,
)
from wayfinder_paths.core.theses.checkpoints import ReviewCheckpoint
from wayfinder_paths.core.theses.review import REVIEW_TOOL, review_report
from wayfinder_paths.tests import test_thesis_assessment
from wayfinder_paths.tests.test_thesis_draft import receipt

case = test_thesis_assessment.case
discovery = test_thesis_assessment.discovery
spec = test_thesis_assessment.spec


@pytest.fixture
def compact_run(case, spec):
    research = {
        k: v
        for k, v in case.items()
        if k not in {"decision", "reason", "decision_basis", "implementation_checks"}
    }
    research["case_basis"] = "economic"
    child = [
        receipt(
            {
                "schema_version": 5,
                "stage": "discovery",
                "spec": spec,
                "research_cases": [research],
                "handoff": {
                    "case_entities": ["network"],
                    "unresolved_entities": [],
                    "reason": "Complete",
                },
            },
            1,
            session="worker",
            agent="thesis-researcher",
        )
    ]
    parent = [
        receipt(
            {
                "schema_version": 5,
                "stage": "judged",
                "construction": {"mode": "directional"},
                "decisions": [
                    {
                        "research_ref": {
                            "session_id": "worker",
                            "checkpoint_id": "t1",
                            "entity": "network",
                        },
                        "entity": "network",
                        "decision": "KEEP",
                        "decision_basis": "economic",
                        "reason": "Comparison",
                    }
                ],
            },
            2,
        )
    ]
    return parent, child


def test_compact_decision_reuses_original_without_mutation(compact_run):
    parent, child = compact_run
    original = deepcopy(child)
    projected, _, errors = projected_records(parent, child)
    assert not errors
    assert projected[0]["checkpoint"]["candidates"][0]["support"] == "Growing usage"
    assert child == original
    assert not assessment_report(parent, child)["errors"]
    index = research_notebook(parent, child)
    assert index["items"][0]["research_refs"] == [
        {"session_id": "worker", "checkpoint_id": "t1", "entity": "network"}
    ]


@pytest.mark.parametrize(
    "field,value",
    [("session_id", "other-tree"), ("checkpoint_id", "missing"), ("entity", "guess")],
)
def test_reference_requires_exact_observed_source(compact_run, field, value):
    parent, child = compact_run
    cp = parent[0]["parts"][0]["state"]["input"]["checkpoint"]
    cp["decisions"][0]["research_ref"][field] = value
    parent = [receipt(cp, 2)]
    assert "earlier saved case" in " ".join(projected_records(parent, child)[2])


def test_reference_cannot_read_future_case(compact_run):
    parent, child = compact_run
    child[0]["parts"][0]["state"]["time"]["end"] = 100
    assert projected_records(parent, child)[2]


def test_bad_reference_can_be_corrected_without_permanent_error(compact_run):
    parent, child = compact_run
    good = deepcopy(parent[0]["parts"][0]["state"]["input"]["checkpoint"])
    bad = deepcopy(good)
    bad["decisions"][0]["research_ref"]["checkpoint_id"] = "missing"
    assert not projected_records([receipt(bad, 2), receipt(good, 3)], child)[2]


def test_unrecorded_worker_gap_remains_visible(compact_run):
    parent, child = compact_run
    child.append(
        {"info": {"sessionID": "capped", "agent": "thesis-researcher"}, "parts": []}
    )
    parent.append(
        receipt(
            {
                "schema_version": 5,
                "stage": "discovery",
                "handoff_gaps": [
                    {
                        "session_id": "capped",
                        "reason": "No receipt after targeted continuation",
                    }
                ],
            },
            3,
        )
    )
    report = assessment_report(parent, child)
    assert not report["errors"]
    assert report["incomplete_handoffs"] == ["capped"]


def test_explicit_reference_links_alias(compact_run):
    parent, child = compact_run
    cp = parent[0]["parts"][0]["state"]["input"]["checkpoint"]
    cp["decisions"][0]["entity"] = "canonical-network"
    parent = [receipt(cp, 2)]
    assert not assessment_report(parent, child)["errors"]


def test_incomplete_handoff_cannot_silently_pass(compact_run):
    parent, child = compact_run
    cp = child[0]["parts"][0]["state"]["input"]["checkpoint"]
    cp["handoff"] = None
    child = [receipt(cp, 1, session="worker", agent="thesis-researcher")]
    assert "incomplete handoff" in " ".join(assessment_report(parent, child)["errors"])
    parent.append(
        receipt(
            {
                "schema_version": 5,
                "stage": "discovery",
                "handoff_gaps": [
                    {
                        "session_id": "worker",
                        "reason": "Resumed once; unfinished research retained",
                    }
                ],
            },
            3,
        )
    )
    report = assessment_report(parent, child)
    assert not report["errors"]
    assert report["incomplete_handoffs"] == ["worker"]


def review(blocking=True):
    cp = ReviewCheckpoint(
        findings=[
            {
                "id": "carry",
                "entity": "network",
                "blocking": blocking,
                "issue": "Spot unchecked",
                "required_change": "Compare spot",
            }
        ]
    )
    return {
        "info": {"sessionID": "reviewer", "agent": "thesis-reviewer"},
        "parts": [
            {
                "id": "review",
                "tool": REVIEW_TOOL,
                "state": {
                    "status": "completed",
                    "time": {"end": 3},
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


@pytest.mark.parametrize(
    "blocking,action,refs,valid",
    [
        (True, "accepted", [], False),
        (False, "accepted", [], True),
        (True, "changed", [], False),
        (True, "evidence", ["invented"], False),
        (True, "evidence", ["public"], True),
        (True, "removed", [], False),
    ],
)
def test_findings_require_substantive_resolution(
    compact_run, blocking, action, refs, valid
):
    parent, child = compact_run
    parent.append(
        receipt(
            {
                "schema_version": 5,
                "stage": "discovery",
                "review_resolutions": [
                    {
                        "review_session_id": "reviewer",
                        "finding_id": "carry",
                        "action": action,
                        "reason": "Resolved",
                        "evidence_part_ids": refs,
                    }
                ],
            },
            5,
        )
    )
    parent.append(
        {
            "parts": [
                {
                    "id": "public",
                    "tool": "wayfinder_core_web_fetch",
                    "state": {
                        "status": "completed",
                        "time": {"end": 4},
                        "output": json.dumps(
                            {
                                "ok": True,
                                "result": {
                                    "results": [
                                        {
                                            "url": "https://example.test",
                                            "contentExcerpt": "Observed alternative evidence",
                                        }
                                    ]
                                },
                            }
                        ),
                    },
                }
            ]
        }
    )
    records = projected_records(parent, child)[0]
    report = review_report(
        parent, [*child, review(blocking)], records, {"NETWORK-USDC"}
    )
    assert (not report["errors"]) == valid


def test_worker_cannot_issue_review(compact_run):
    parent, child = compact_run
    fake = review()
    fake["info"]["agent"] = "thesis-researcher"
    assert (
        "Reviewer must record"
        in review_report(
            parent, [*child, fake], projected_records(parent, child)[0], set()
        )["errors"][0]
    )
