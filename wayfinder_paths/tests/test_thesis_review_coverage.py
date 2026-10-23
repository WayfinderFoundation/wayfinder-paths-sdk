"""Current comparison coverage, not an automated investment-opinion judge."""

import hashlib
import json
from copy import deepcopy
from typing import Any

import pytest

from wayfinder_paths.core.theses.assessment import (
    projected_records,
    research_notebook,
    research_observations,
)
from wayfinder_paths.core.theses.checkpoints import ReviewCheckpoint
from wayfinder_paths.core.theses.review import review_report
from wayfinder_paths.tests import test_thesis_decisions as fixtures
from wayfinder_paths.tests.test_thesis_draft import receipt
from wayfinder_paths.tests.test_thesis_handoffs import signoff

case = fixtures.case
discovery = fixtures.discovery
spec = fixtures.spec
compact_run = fixtures.compact_run
decision_run = fixtures.decision_run


def report(
    parent: list[dict],
    children: list[dict],
    revision: str = "current",
    *,
    variants: list[dict] | None = None,
) -> dict[str, Any]:
    records, _, errors = projected_records(parent, children)
    assert not errors
    return review_report(
        parent,
        children,
        records,
        {"NETWORK-USDC"},
        revision=revision,
        variants=variants,
    )


def checked(
    parent: list[dict],
    children: list[dict],
    number: int = 10,
    *,
    variants: list[dict] | None = None,
) -> dict[str, Any]:
    required = report(parent, children, variants=variants)["case_checks"]["required"]
    message = signoff("current", number)
    for view, result in (
        (
            "cases",
            research_notebook(
                parent,
                children,
                entities=[row["case_ref"]["entity"] for row in required],
                history=False,
                compact=True,
            ),
        ),
        (
            "evidence",
            research_observations(
                parent,
                children,
                reads=[{"part_id": "proof", "result_path": ["results", 0]}],
            ),
        ),
    ):
        message["parts"].insert(
            0,
            {
                "tool": "thesis_notebook",
                "state": {
                    "status": "completed",
                    "input": {"view": view},
                    "time": {"end": number - 1},
                    "output": json.dumps(result),
                },
            },
        )
    cp = ReviewCheckpoint(
        reviewed_revision="current",
        findings=[],
        case_checks=[
            {
                "case_ref": row["case_ref"],
                "input_digest": row["input_digest"],
                "evidence_reads": [{"part_id": "proof", "result_path": ["results", 0]}],
                "conclusion": "supports",
                "reason": "Growth-linked demand versus the alternative; no cash distribution requirement",
            }
            for row in required
        ],
    )
    message["parts"][-1]["state"].update(
        input={"checkpoint": cp.model_dump(mode="json")},
        output=json.dumps(
            {
                "ok": True,
                "result": {
                    "sha256": hashlib.sha256(cp.receipt_json().encode()).hexdigest()
                },
            }
        ),
    )
    return message


@pytest.fixture
def modern_run(decision_run: fixtures.Run) -> fixtures.Run:
    parent, children = decision_run
    raw = deepcopy(parent[-1]["parts"][0]["state"]["input"]["checkpoint"])
    raw["schema_version"] = 8
    parent[-1] = receipt(raw, 2)
    return parent, children


@pytest.mark.parametrize("missing", ["cases", "evidence", None])
def test_check_requires_actual_current_case_and_source_reads(
    modern_run: fixtures.Run, missing: str | None
) -> None:
    parent, children = modern_run
    message = checked(parent, children)
    if missing:
        message["parts"] = [
            p for p in message["parts"] if p["state"]["input"].get("view") != missing
        ]
    result = report(parent, [*children, message])
    assert result["case_checks"]["required"][0]["checked"] is (missing is None)
    assert bool(result["errors"]) is (missing is not None)


def test_empty_signoff_cannot_skip_checks_but_legacy_runs_are_unchanged(
    modern_run: fixtures.Run,
) -> None:
    parent, children = modern_run
    assert report(parent, [*children, signoff("current", 10)])["case_checks"]["errors"]
    raw = deepcopy(parent[-1]["parts"][0]["state"]["input"]["checkpoint"])
    raw["schema_version"] = 7
    parent[-1] = receipt(raw, 2)
    assert "case_checks" not in report(parent, children)


def test_unchanged_checks_survive_new_revision_but_corrected_cases_require_review(
    modern_run: fixtures.Run,
) -> None:
    parent, children = modern_run
    children.append(checked(parent, children))
    children.append(signoff("new", 12))
    assert not report(parent, children, "new")["errors"]
    raw = deepcopy(parent[-1]["parts"][0]["state"]["input"]["checkpoint"])
    raw["decisions"][0]["updated_research"] = {
        "value_capture": "Revised mechanism, not immutable buybacks"
    }
    parent.append(receipt(raw, 13))
    children.append(signoff("newer", 16))
    assert report(parent, children, "newer")["case_checks"]["errors"]


def test_corrected_winner_invalidates_rejected_dependent_comparison(
    modern_run: fixtures.Run,
) -> None:
    parent, children = modern_run
    raw = deepcopy(parent[-1]["parts"][0]["state"]["input"]["checkpoint"])
    research = deepcopy(
        children[0]["parts"][0]["state"]["input"]["checkpoint"]["research_cases"][0]
    )
    research.update(
        entity="challenger", name="Emerging challenger", instruments=["CHALLENGER-USDC"]
    )
    children.append(
        receipt(
            {"schema_version": 6, "stage": "discovery", "research_cases": [research]},
            3,
            session="worker",
            agent="thesis-researcher",
        )
    )
    decision = deepcopy(raw["decisions"][0])
    decision.update(
        entity="challenger",
        research_ref={
            "session_id": "worker",
            "checkpoint_id": "t3",
            "entity": "challenger",
        },
        decision="REJECT",
        comparison_refs=[
            {"session_id": "parent", "checkpoint_id": "t2", "entity": "network"}
        ],
    )
    parent.append(
        receipt({"schema_version": 8, "stage": "judged", "decisions": [decision]}, 4)
    )
    # The selected winner references its strongest omitted challenger too.
    raw["decisions"][0]["comparison_refs"] = [
        {"session_id": "parent", "checkpoint_id": "t4", "entity": "challenger"}
    ]
    parent.append(receipt(raw, 5))
    children.append(checked(parent, children))
    assert not report(parent, children)["errors"]
    raw["decisions"][0]["updated_research"] = {
        "value_capture": "One-year lock, not permanent removal"
    }
    parent.append(receipt(raw, 13))
    result = report(parent, children)
    assert {
        row["case_ref"]["entity"]
        for row in result["case_checks"]["required"]
        if not row["checked"]
    } == {"network", "challenger"}
    assert result["decision_evidence"]["comparison_updates"]


def test_changed_economic_exposure_invalidates_prior_check(
    modern_run: fixtures.Run,
) -> None:
    from wayfinder_paths.tests.test_thesis_quantification import variant

    parent, children = modern_run
    draft = variant(instrument_id="NETWORK-USDC", capital_bps=10000).model_dump(
        mode="json"
    )
    children.append(checked(parent, children, variants=[draft]))
    assert not report(parent, children, variants=[draft])["errors"]
    draft["positions"][0]["leverage"] = 2
    assert report(parent, children, variants=[draft])["case_checks"]["errors"]


@pytest.mark.parametrize("path", [None, ["results", 1], ["results", 0, "text"]])
def test_check_cannot_claim_unread_sections_as_complete_sources(
    modern_run: fixtures.Run, path: list[str | int] | None
) -> None:
    parent, children = modern_run
    message = checked(parent, children)
    state = message["parts"][-1]["state"]
    raw = state["input"]["checkpoint"]
    raw["case_checks"][0]["evidence_reads"][0]["result_path"] = path
    cp = ReviewCheckpoint.model_validate(raw)
    state["output"] = json.dumps(
        {
            "ok": True,
            "result": {
                "sha256": hashlib.sha256(cp.receipt_json().encode()).hexdigest()
            },
        }
    )
    result = report(parent, [*children, message])
    assert result["case_checks"]["required"][0]["checked"] is (
        path == ["results", 0, "text"]
    )
