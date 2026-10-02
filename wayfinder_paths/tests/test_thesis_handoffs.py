import hashlib
import json
from copy import deepcopy
from typing import Any

import pytest

from wayfinder_paths.core.theses.assessment import (
    DISCOVERY_TOOL,
    assessment_report,
    checkpoints,
    projected_records,
    research_notebook,
    research_observations,
)
from wayfinder_paths.core.theses.checkpoints import (
    DiscoveryCheckpoint,
    ResearchCheckpoint,
    ReviewCheckpoint,
)
from wayfinder_paths.core.theses.draft import draft_context, draft_status
from wayfinder_paths.core.theses.review import REVIEW_TOOL, review_report
from wayfinder_paths.tests import test_thesis_assessment
from wayfinder_paths.tests.test_thesis_draft import receipt

case = test_thesis_assessment.case
discovery = test_thesis_assessment.discovery
spec = test_thesis_assessment.spec


def test_v5_worker_can_append_handoff_without_repeating_frozen_spec() -> None:
    worker = DiscoveryCheckpoint(
        schema_version=5,
        handoff={
            "case_entities": ["network"],
            "unresolved_entities": [],
            "reason": "Complete",
        },
    )
    checkpoint = ResearchCheckpoint.model_validate(worker.model_dump())
    assert checkpoint.spec is None
    assert checkpoint.handoff.case_entities == ["network"]


def test_legacy_worker_still_requires_spec() -> None:
    with pytest.raises(ValueError, match="require spec"):
        DiscoveryCheckpoint(schema_version=3)


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [3, 5])
@pytest.mark.parametrize("explicit", [True, False])
async def test_worker_receipts_preserve_explicit_and_omitted_versions(
    spec: dict, version: int, explicit: bool
) -> None:
    from wayfinder_paths.mcp.tools.thesis_checkpoint import research_thesis_discovery

    raw = {"spec": spec} if version == 3 else {"discoveries": []}
    if explicit:
        raw["schema_version"] = version
    checkpoint = DiscoveryCheckpoint.model_validate({**raw, "schema_version": version})
    output = await research_thesis_discovery(checkpoint)
    if version == 3:
        # Historical receipts had no effective-version field.
        output["result"].pop("schema_version")
    else:
        assert DiscoveryCheckpoint.model_validate(raw).schema_version == 5
        assert output["result"]["schema_version"] == 5
    message = {
        "parts": [
            {
                "tool": DISCOVERY_TOOL,
                "state": {
                    "status": "completed",
                    "input": {"checkpoint": raw},
                    "output": json.dumps(output),
                },
            }
        ]
    }
    records = checkpoints([message])
    assert len(records) == 1
    assert records[0]["checkpoint"]["schema_version"] == version
    output["result"]["schema_version"] = 3 if version == 5 else 5
    message["parts"][0]["state"]["output"] = json.dumps(output)
    assert not checkpoints([message])


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


@pytest.mark.parametrize("version,required", [(5, False), (6, True)])
@pytest.mark.parametrize(
    "disposition", [None, "out_of_scope", "needs_evidence", "assessed"]
)
def test_assigned_parent_leads_cannot_disappear(
    compact_run: tuple[list[dict], list[dict]],
    discovery: dict,
    version: int,
    required: bool,
    disposition: str | None,
) -> None:
    parent, child = compact_run
    lead = {**discovery, "entity": "assigned-lead"}
    parent.append(
        receipt(
            {"schema_version": version, "stage": "discovery", "discoveries": [lead]}, 3
        )
    )
    if disposition:
        parent.append(
            receipt(
                {
                    "schema_version": version,
                    "stage": "judged",
                    "construction": {"mode": "directional"},
                    "discovery_dispositions": [
                        {
                            "entities": ["assigned-lead"],
                            "status": disposition,
                            "candidate_entity": "network"
                            if disposition == "assessed"
                            else None,
                            "reason": "Explicit triage, not silently omitted",
                        }
                    ],
                },
                4,
            )
        )
    report = assessment_report(parent, child)
    assert ("assigned-lead" in report["missing_entities"]) == (
        required and disposition is None
    )
    assert report["assigned_entities"] == (["assigned-lead"] if required else [])
    if disposition:
        assert not report["errors"]


def test_v6_inherits_construction_without_changing_recorded_receipts(
    compact_run: tuple[list[dict], list[dict]],
    spec: dict,
) -> None:
    parent, child = compact_run
    interpretation = receipt(
        {
            "schema_version": 6,
            "stage": "interpretation",
            "spec": spec,
            "construction": {"mode": "directional"},
        },
        0,
    )
    decision = deepcopy(parent[0]["parts"][0]["state"]["input"]["checkpoint"])
    decision.update(schema_version=6, construction=None)
    parent = [interpretation, receipt(decision, 2)]
    original = deepcopy(parent)
    records, _, errors = projected_records(parent, child)
    assert not errors
    assert records[-1]["checkpoint"]["construction"]["mode"] == "directional"
    assert parent == original
    assert draft_context(parent, child)[1]["construction"]["mode"] == "directional"
    with pytest.raises(ValueError, match="construction"):
        ResearchCheckpoint(schema_version=6, stage="interpretation", spec=spec)


def signoff(
    revision: str,
    number: int,
    *,
    session: str = "reviewer",
    read: bool = True,
    read_at: int | None = None,
) -> dict[str, Any]:
    checkpoint = ReviewCheckpoint(findings=[], reviewed_revision=revision)
    message: dict[str, Any] = {
        "info": {"sessionID": session, "agent": "thesis-reviewer", "finish": "stop"},
        "parts": [],
    }
    if read:
        message["parts"].append(
            {
                "tool": "thesis_notebook",
                "state": {
                    "status": "completed",
                    "time": {"end": read_at or number - 1},
                    "input": {"view": "draft"},
                    "output": json.dumps(
                        {"proposal": {}, "review": {"revision": revision}}
                    ),
                },
            }
        )
    message["parts"].append(
        {
            "tool": REVIEW_TOOL,
            "state": {
                "status": "completed",
                "time": {"end": number},
                "input": {"checkpoint": checkpoint.model_dump()},
                "output": json.dumps(
                    {
                        "ok": True,
                        "result": {
                            "sha256": hashlib.sha256(
                                checkpoint.receipt_json().encode()
                            ).hexdigest()
                        },
                    }
                ),
            },
        }
    )
    return message


@pytest.mark.parametrize(
    "read,read_at,valid", [(False, None, False), (True, 8, False), (True, 6, True)]
)
def test_revision_signoff_requires_same_reviewer_to_read_current_draft(
    compact_run: tuple[list[dict], list[dict]],
    read: bool,
    read_at: int | None,
    valid: bool,
) -> None:
    parent, child = compact_run
    child.append(signoff("current", 7, read=read, read_at=read_at))
    report = review_report(
        parent, child, projected_records(parent, child)[0], set(), revision="current"
    )
    assert (not report["errors"]) == valid
    assert (report["changes_since_review"] is not None) == valid
    assert review_report(
        parent, child, projected_records(parent, child)[0], set(), revision="changed"
    )["errors"]


def test_review_change_index_uses_saved_read_not_later_signoff(
    compact_run: tuple[list[dict], list[dict]],
) -> None:
    parent, child = compact_run
    child.append(signoff("old", 7, read_at=5))
    update = deepcopy(parent[0]["parts"][0]["state"]["input"]["checkpoint"])
    update["decisions"][0]["reason"] = "Changed comparison"
    parent.append(receipt(update, 6))
    parent.append(
        receipt(
            {
                "schema_version": 6,
                "stage": "draft",
                "draft": {"remove_components": ["old"]},
                "discovery_dispositions": [
                    {
                        "entities": ["alias"],
                        "status": "needs_evidence",
                        "reason": "Still unresolved",
                    }
                ],
            },
            8,
        )
    )
    failed = receipt(update, 9)
    failed["parts"][0]["state"]["output"] = json.dumps({"ok": False})
    parent.append(failed)
    # A later forged or unread sign-off cannot hide the updates.
    child.append(signoff("unread", 12, read=False))
    report = review_report(
        parent, child, projected_records(parent, child)[0], set(), revision="current"
    )
    changes = report["changes_since_review"]
    assert changes["baseline_revision"] == "old"
    assert changes["baseline_read_at_ms"] == 5
    assert changes["parent_checkpoint_ids"] == ["t6", "t8"]
    assert changes["parent_entity_keys"] == ["alias", "network"]
    assert changes["parent_stages"] == ["draft", "judged"]
    assert report["errors"]  # This index cannot certify the changed revision.
    child.append(signoff("current", 14))
    refreshed = review_report(
        parent, child, projected_records(parent, child)[0], set(), revision="current"
    )["changes_since_review"]
    assert refreshed["baseline_revision"] == "current"
    assert refreshed["parent_checkpoint_ids"] == []
    assert refreshed["parent_entity_keys"] == []


@pytest.mark.parametrize("change", ["decision", "resolution", "draft", "research"])
def test_v6_review_is_invalidated_by_later_selection_or_resolution(
    compact_run: tuple[list[dict], list[dict]],
    spec: dict,
    discovery: dict,
    change: str,
) -> None:
    parent, child = compact_run
    parent.insert(
        0,
        receipt(
            {
                "schema_version": 6,
                "stage": "interpretation",
                "spec": spec,
                "construction": {"mode": "directional"},
            },
            0,
        ),
    )
    original_revision = draft_context(parent, child)[1]["review"]["revision"]
    child.append(signoff(original_revision, 7))
    assert not draft_context(parent, child)[1]["review"]["errors"]
    update = {"schema_version": 6, "stage": "discovery"}
    if change == "decision":
        update = deepcopy(parent[1]["parts"][0]["state"]["input"]["checkpoint"])
        update["decisions"][0]["reason"] = "Relabeled as generic infrastructure beta"
    elif change == "resolution":
        # Even a correct structural resolution needs a delta sign-off. A made-up
        # finding remains an independent error; it cannot be waved through by review.
        update["review_resolutions"] = [
            {
                "review_session_id": "reviewer",
                "finding_id": "missing",
                "action": "accepted",
                "reason": "Acknowledged",
            }
        ]
    elif change == "draft":
        update.update(stage="draft", draft={"remove_components": ["old"]})
    else:
        child.append(
            receipt(
                {
                    "schema_version": 6,
                    "stage": "discovery",
                    "discoveries": [{**discovery, "entity": "new"}],
                },
                8,
                session="worker",
                agent="thesis-researcher",
            )
        )
    if change != "research":
        parent.append(receipt(update, 8))
    new = draft_context(parent, child)[1]["review"]
    assert new["revision"] != original_revision
    assert any("sign-off" in e for e in new["errors"])
    child.append(signoff(new["revision"], 10))
    approved = draft_context(parent, child)[1]["review"]
    assert approved["revision"] == new["revision"]  # No circular invalidation.
    assert not any("sign-off" in e for e in approved["errors"])
    if change == "resolution":
        assert any("unknown review finding" in e for e in approved["errors"])


def test_legacy_review_receipt_remains_readable() -> None:
    old = {"findings": []}
    checkpoint = ReviewCheckpoint.model_validate(old)
    assert json.loads(checkpoint.receipt_json()) == old
    message = signoff("unused", 5)
    state = message["parts"][-1]["state"]
    state["input"]["checkpoint"] = old
    state["output"] = json.dumps(
        {
            "ok": True,
            "result": {
                "sha256": hashlib.sha256(
                    checkpoint.receipt_json().encode()
                ).hexdigest(),
            },
        }
    )
    assert not review_report([], [message], [], set())["errors"]
    assert review_report([], [message], [], set(), revision="current")["errors"]


def test_replacement_reviewer_cannot_bypass_delta_review() -> None:
    children = [signoff("old", 3), signoff("current", 5, session="replacement")]
    errors = review_report([], children, [], set(), revision="current")["errors"]
    assert any("one native reviewer" in error for error in errors)


def test_review_read_from_another_session_cannot_supply_signoff() -> None:
    signed = signoff("current", 5, read=False)
    other = signoff("current", 4, session="other")
    other["parts"].pop()  # Only a draft read, not a second review receipt.
    assert review_report([], [other, signed], [], set(), revision="current")["errors"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "include_cases,include_handoff", [(True, False), (True, True), (False, True)]
)
async def test_worker_receipt_counts_research_separately_from_parent_judgments(
    compact_run: tuple[list[dict], list[dict]],
    include_cases: bool,
    include_handoff: bool,
) -> None:
    from wayfinder_paths.mcp.tools.thesis_checkpoint import research_thesis_discovery

    _, child = compact_run
    raw = deepcopy(child[0]["parts"][0]["state"]["input"]["checkpoint"])
    if not include_cases:
        raw["research_cases"] = []
    if not include_handoff:
        raw["handoff"] = None
    checkpoint = DiscoveryCheckpoint.model_validate(
        {
            key: value
            for key, value in raw.items()
            if key in DiscoveryCheckpoint.model_fields
        }
    )
    response = await research_thesis_discovery(checkpoint)
    assert response["ok"] is True
    assert response["result"]["candidate_count"] == 0
    assert response["result"]["research_case_count"] == int(include_cases)
    assert response["result"]["handoff_recorded"] is include_handoff
    assert "research_cases" not in response["result"]  # Do not echo full essays.
    assert response["result"]["execution_authorized"] is False


def test_status_evidence_ids_are_identifiable_paged_and_not_silently_lost(
    compact_run: tuple[list[dict], list[dict]],
) -> None:
    parent, child = compact_run
    parent.append(
        {
            "parts": [
                {
                    "type": "tool",
                    "id": f"read-{index}",
                    "tool": "wayfinder_core_web_fetch",
                    "state": {
                        "status": "completed",
                        "time": {"end": index + 10},
                        "input": {
                            "urls": [f"https://example.test/venue-{index}"],
                            "query": "fee capture",
                            "unrelated_config": "do not expose",
                        },
                        "output": json.dumps(
                            {
                                "ok": True,
                                "result": {"results": [{"contentExcerpt": "Fee docs"}]},
                            }
                        ),
                    },
                }
                for index in range(31)
            ]
        }
    )
    first = draft_status(parent, child)["review"]
    assert first["public_observations_page"] == {
        "total": 31,
        "next_offset": 25,
        "order": "newest_first",
    }
    second = draft_status(parent, child, offset=25)["review"]
    assert second["public_observations_page"]["next_offset"] is None
    rows = first["public_observations"] + second["public_observations"]
    assert [row["part_id"] for row in rows] == [
        f"read-{i}" for i in reversed(range(31))
    ]
    assert "https://example.test/venue-30" in rows[0]["request_summary"]
    assert "unrelated_config" not in json.dumps(rows)
    # Paging the model's view never limits the evidence used by publication.
    assert len(draft_context(parent, child)[1]["review"]["public_observations"]) == 31
    evidence = research_observations(parent, child, part_ids=["read-30", "read-0"])
    assert [row["part_id"] for row in evidence["observations"]] == ["read-30", "read-0"]
    assert evidence["observations"][0]["result"] == {
        "results": [{"contentExcerpt": "Fee docs"}]
    }
    assert not evidence["unavailable_part_ids"]
    assert "unrelated_config" not in json.dumps(evidence)


def test_case_projection_keeps_classification_and_identifies_current_assessment(
    compact_run: tuple[list[dict], list[dict]],
) -> None:
    parent, child = compact_run
    update = deepcopy(parent[0]["parts"][0]["state"]["input"]["checkpoint"])
    updated_research = deepcopy(
        child[0]["parts"][0]["state"]["input"]["checkpoint"]["research_cases"][0]
    )
    updated_research["case_basis"] = "narrative"
    update["decisions"][0].update(
        updated_research=updated_research,
        decision_basis="portfolio",
        reason="Observed catalyst, not a fee claim",
    )
    parent.append(receipt(update, 3))
    original = deepcopy((parent, child))
    row = research_notebook(parent, child, entities=["network"], fields=["reason"])[
        "cases"
    ][0]
    assert len(row["records"]) == 3  # Preserve research and both parent judgments.
    assert [r["current_assessment"] for r in row["records"]] == [False, False, True]
    assert row["records"][0]["case"]["case_basis"] == "economic"
    assert row["records"][-1]["case"] == {
        "observed_identifiers": updated_research["observed_identifiers"],
        "effect_order": updated_research["effect_order"],
        "case_basis": "narrative",
        "decision": "KEEP",
        "decision_basis": "portfolio",
        "reason": "Observed catalyst, not a fee claim",
    }
    # A historical page must never label its last old verdict as current.
    historical = research_notebook(parent, child, entities=["network"], limit=2)
    assert not any(r["current_assessment"] for r in historical["cases"][0]["records"])
    assert (parent, child) == original


def test_conflicting_worker_metrics_survive_focused_case_read(compact_run):
    parent, child = compact_run
    checkpoint = deepcopy(child[0]["parts"][0]["state"]["input"]["checkpoint"])
    original_case = checkpoint["research_cases"][0]
    original_case["observed_identifiers"] = [
        "venue-perps total30d dailyFees=6000 USD; parent#venue; source: provider",
        "venue-new total30d dailyHoldersRevenue=0 USD; NewChain; source: provider",
    ]
    original_case["support"] = "Main venue receives fees"
    original_case["counterevidence"] = "New deployment reports no holder revenue"
    child = [receipt(checkpoint, 1, session="worker", agent="thesis-researcher")]
    before = deepcopy(child)
    row = research_notebook(parent, child, entities=["network"], fields=["reason"])[
        "cases"
    ][0]
    for record in row["records"]:
        assert (
            record["case"]["observed_identifiers"]
            == original_case["observed_identifiers"]
        )
    assert child == before


def test_evidence_retains_fee_and_holder_flows_without_adding_them():
    messages = [
        {
            "parts": [
                {
                    "id": metric,
                    "tool": "wayfinder_research_defillama_free",
                    "state": {
                        "status": "completed",
                        "input": {"dataset": "fees_overview", "dataType": metric},
                        "output": json.dumps(
                            {
                                "ok": True,
                                "result": {
                                    "items": [
                                        {"slug": "venue-perps", "total30d": value}
                                    ],
                                },
                            }
                        ),
                    },
                }
                for metric, value in [
                    ("dailyFees", 6000),
                    ("dailyHoldersRevenue", 4000),
                ]
            ]
        }
    ]
    report = research_observations(
        messages, [], part_ids=["dailyFees", "dailyHoldersRevenue"]
    )
    assert [r["result"]["items"][0]["total30d"] for r in report["observations"]] == [
        6000,
        4000,
    ]
    assert "dailyFees" in report["observations"][0]["request_summary"]
    assert "dailyHoldersRevenue" in report["observations"][1]["request_summary"]


@pytest.mark.parametrize(
    "output",
    [
        "not JSON",
        "null",
        "[]",
        '{"ok":false,"result":{"text":"failure"}}',
        '{"ok":true,"result":{}}',
        '{"ok":true,"result":{"results":null}}',
        '{"ok":true,"result":{"results":[{"url":"no readable text"}]}}',
    ],
)
def test_unusable_saved_evidence_is_explicitly_unavailable(output):
    messages = [
        {
            "parts": [
                {
                    "id": "read",
                    "tool": "wayfinder_core_web_fetch",
                    "state": {"status": "completed", "output": output},
                }
            ]
        }
    ]
    report = research_observations(messages, [], part_ids=["read", "unknown"])
    assert report["observations"] == []
    assert report["unavailable_part_ids"] == ["read", "unknown"]


@pytest.mark.parametrize(
    "tool,status",
    [
        ("wallets", "completed"),
        ("wayfinder_core_web_fetch", "error"),
        ("wayfinder_core_web_fetch", "running"),
    ],
)
def test_private_or_incomplete_reads_cannot_be_evidence(tool, status):
    messages = [
        {
            "parts": [
                {
                    "id": "read",
                    "tool": tool,
                    "state": {
                        "status": status,
                        "output": json.dumps(
                            {
                                "ok": True,
                                "result": {
                                    "results": [{"text": "must not expose"}],
                                },
                            }
                        ),
                    },
                }
            ]
        }
    ]
    report = research_observations(messages, [], part_ids=["read"])
    assert report["unavailable_part_ids"] == ["read"]
    assert "must not expose" not in json.dumps(report)


@pytest.mark.parametrize("part_ids", [[], ["a", "b", "c", "d"], [""], "read"])
def test_evidence_requires_bounded_exact_ids(part_ids):
    with pytest.raises(ValueError, match="part_ids"):
        research_observations([], [], part_ids=part_ids)


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


@pytest.mark.parametrize(
    "field,keys,expected",
    [
        ("case_entities", [], "case_entities missing: network"),
        ("case_entities", ["ghost"], "case_entities unexpected: ghost"),
        ("unresolved_entities", ["ghost"], "unresolved_entities unexpected: ghost"),
    ],
)
def test_handoff_error_identifies_the_mismatched_inventory(
    compact_run: tuple[list[dict], list[dict]],
    field: str,
    keys: list[str],
    expected: str,
) -> None:
    parent, child = compact_run
    checkpoint = deepcopy(child[0]["parts"][0]["state"]["input"]["checkpoint"])
    checkpoint["handoff"][field] = keys
    child = [receipt(checkpoint, 1, session="worker", agent="thesis-researcher")]
    assert expected in " ".join(assessment_report(parent, child)["errors"])


@pytest.mark.asyncio
async def test_live_review_requires_nested_revision_but_legacy_model_remains_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wayfinder_paths.mcp.tools.thesis_checkpoint import research_thesis_review

    monkeypatch.setattr(
        "wayfinder_paths.mcp.utils._report_tool_metric", lambda *args: None
    )
    legacy = ReviewCheckpoint(findings=[])
    assert legacy.receipt_json() == '{"findings":[]}'
    result = await research_thesis_review(legacy)
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_argument"
    assert "checkpoint.reviewed_revision" in result["error"]["message"]

    current = ReviewCheckpoint(findings=[], reviewed_revision="revision-1")
    result = await research_thesis_review(current)
    assert result["ok"] is True
    assert result["result"]["reviewed_revision"] == "revision-1"
    assert result["result"]["finding_keys"] == []
    assert (
        result["result"]["sha256"]
        == hashlib.sha256(current.receipt_json().encode()).hexdigest()
    )
    current = ReviewCheckpoint(
        reviewed_revision="revision-2",
        findings=[
            {
                "id": "exact-stable-finding-id",
                "entity": "candidate",
                "blocking": True,
                "issue": "Unverified direction",
                "required_change": "Check the contract payoff",
            }
        ],
    )
    result = await research_thesis_review(current)
    assert result["result"]["finding_keys"] == [
        {
            "finding_id": "exact-stable-finding-id",
            "entity": "candidate",
            "blocking": True,
        }
    ]


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
    "blocking,action,refs,updated_at,valid",
    [
        (True, "accepted", [], [], False),
        (False, "accepted", [], [], True),
        (True, "changed", [], [], False),
        (True, "changed", ["public"], [], False),
        (True, "changed", ["public"], [2], False),
        (True, "changed", ["public"], [4], True),
        (True, "changed", ["public"], [6], False),
        (True, "changed", ["public"], [4, 6], True),
        (True, "evidence", ["invented"], [], False),
        (True, "evidence", ["public"], [], True),
        (True, "removed", [], [], False),
    ],
)
def test_findings_require_substantive_resolution(
    compact_run: tuple[list[dict], list[dict]],
    blocking: bool,
    action: str,
    refs: list[str],
    updated_at: list[int],
    valid: bool,
) -> None:
    parent, child = compact_run
    original_child = deepcopy(child)
    for timestamp in updated_at:
        decision = deepcopy(parent[0]["parts"][0]["state"]["input"]["checkpoint"])
        decision["decisions"][0]["reason"] = "Corrected implementation comparison"
        parent.append(receipt(decision, timestamp))
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
    assert child == original_child


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


@pytest.mark.parametrize(
    "wrong_session,wrong_finding,corrected_at,blocking,ambiguous,repeated,cleared",
    [
        ("typo", "carry", 5, False, False, False, True),
        ("typo", "carry", 5, False, False, True, True),
        ("typo", "carry", None, False, False, False, False),
        ("typo", "carry", 3, False, False, False, False),
        ("typo", "carry", 5, True, False, False, False),
        ("typo", "carry", 5, False, True, False, False),
        ("typo", "unknown-finding", 5, False, False, False, False),
        ("other-reviewer", "carry", 5, False, False, False, False),
    ],
)
def test_only_later_valid_unambiguous_resolution_can_correct_session_typo(
    compact_run: tuple[list[dict], list[dict]],
    wrong_session: str,
    wrong_finding: str,
    corrected_at: int | None,
    blocking: bool,
    ambiguous: bool,
    repeated: bool,
    cleared: bool,
) -> None:
    parent, child = compact_run
    child.append(review(blocking))
    if ambiguous or wrong_session == "other-reviewer":
        other = review(False)
        other["info"]["sessionID"] = "other-reviewer"
        if not ambiguous:
            checkpoint = ReviewCheckpoint(findings=[])
            state = other["parts"][0]["state"]
            state["input"]["checkpoint"] = checkpoint.model_dump()
            state["output"] = json.dumps(
                {
                    "ok": True,
                    "result": {
                        "sha256": hashlib.sha256(
                            checkpoint.model_dump_json().encode()
                        ).hexdigest()
                    },
                }
            )
        child.append(other)
    attempts = [(wrong_session, wrong_finding, 4)]
    if corrected_at is not None:
        attempts.append(("reviewer", "carry", corrected_at))
    if repeated:
        attempts.append((wrong_session, wrong_finding, 6))
    for session, finding, timestamp in attempts:
        parent.append(
            receipt(
                {
                    "schema_version": 5,
                    "stage": "discovery",
                    "review_resolutions": [
                        {
                            "review_session_id": session,
                            "finding_id": finding,
                            "action": "accepted",
                            "reason": "Disclosed nonblocking uncertainty",
                        }
                    ],
                },
                timestamp,
            )
        )
    original = deepcopy((parent, child))
    report = review_report(parent, child, projected_records(parent, child)[0], set())
    unknown = [e for e in report["errors"] if "unknown review finding" in e]
    assert (not unknown) == cleared
    if cleared:
        assert not report["errors"]
    assert (parent, child) == original


@pytest.mark.parametrize(
    "corrected,blocking,signed_revision,read,extra_reviewer,cleared",
    [
        (True, False, "current", True, False, True),
        (False, False, "current", True, False, False),
        (True, True, "current", True, False, False),
        (True, False, "stale", True, False, False),
        (True, False, "current", False, False, False),
        (True, False, "current", True, True, False),
    ],
)
def test_unknown_resolution_ids_remain_audited_after_reviewed_corrections(
    compact_run: tuple[list[dict], list[dict]],
    corrected: bool,
    blocking: bool,
    signed_revision: str,
    read: bool,
    extra_reviewer: bool,
    cleared: bool,
) -> None:
    parent, child = compact_run
    child.append(review(blocking))
    resolutions = [
        {
            "review_session_id": "reviewer",
            "finding_id": "f1",
            "action": "accepted",
            "reason": "Mistyped shorthand; cannot close the real finding",
        }
    ]
    if corrected:
        resolutions.append(
            {**resolutions[0], "finding_id": "carry", "reason": "Disclosed uncertainty"}
        )
    parent.append(
        receipt(
            {"schema_version": 6, "stage": "judged", "review_resolutions": resolutions},
            5,
        )
    )
    child.append(signoff(signed_revision, 7, read=read))
    if extra_reviewer:
        child.append(signoff("current", 8, session="replacement"))
    original = deepcopy((parent, child))
    report = review_report(
        parent, child, projected_records(parent, child)[0], set(), revision="current"
    )
    assert (not report["errors"]) == cleared
    assert bool(report["warnings"]) == cleared
    assert report["findings"][0]["resolved"] == (corrected and not blocking)
    if cleared:
        assert "reviewer/f1" in report["warnings"][0]
        assert "does not resolve" in report["warnings"][0]
    assert (parent, child) == original
