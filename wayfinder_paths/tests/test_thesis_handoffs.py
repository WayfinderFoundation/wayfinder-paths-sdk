import hashlib
import json
from copy import deepcopy
from typing import Any, Literal

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
from wayfinder_paths.tests.test_thesis_draft import observation, receipt

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


@pytest.mark.parametrize("revision_field", [None, "current", "omitted"])
def test_legacy_findings_keep_assessment_scope_and_bind_new_scope(
    revision_field: str | None,
) -> None:
    message = review()
    state = message["parts"][0]["state"]
    raw = state["input"]["checkpoint"]
    raw["findings"][0].pop("scope", None)
    if revision_field == "omitted":
        raw.pop("reviewed_revision", None)
    else:
        raw["reviewed_revision"] = revision_field
    old_hash = hashlib.sha256(
        json.dumps(raw, separators=(",", ":")).encode()
    ).hexdigest()
    state["output"] = json.dumps({"ok": True, "result": {"sha256": old_hash}})
    report = review_report([], [message], [], set())
    assert report["findings"][0]["scope"] == "assessment"
    # A legacy receipt must not authorize changing which artifact needs repair.
    raw["findings"][0]["scope"] = "draft"
    assert not review_report([], [message], [], set())["findings"]
    checkpoint = ReviewCheckpoint.model_validate(raw)
    assert hashlib.sha256(checkpoint.receipt_json().encode()).hexdigest() != old_hash


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
        "claims": [],
        "comparison_refs": [],
    }
    # A historical page must never label its last old verdict as current.
    historical = research_notebook(parent, child, entities=["network"], limit=2)
    assert not any(r["current_assessment"] for r in historical["cases"][0]["records"])
    assert (parent, child) == original


def test_case_sources_link_exact_saved_documents_without_summarizing(
    compact_run: tuple[list[dict], list[dict]],
) -> None:
    parent, child = compact_run
    for number, text in [(10, "Growth observed"), (11, "Contrary observation")]:
        child.append(
            observation(
                "wayfinder_core_web_fetch",
                {
                    "results": [
                        {
                            "url": "https://unrelated.test",
                            "contentExcerpt": "Other data",
                        },
                        {"url": "https://example.test", "contentExcerpt": text},
                    ]
                },
                number,
            )
        )
        child[-1]["parts"][0]["id"] = f"e{number}"
    original = deepcopy((parent, child))
    row = research_notebook(parent, child, entities=["network"], fields=["reason"])[
        "cases"
    ][0]
    assert row["source_reads"] == [
        {
            "url": "https://example.test",
            "part_id": f"e{n}",
            "result_path": ["results", 1],
        }
        for n in (10, 11)
    ]
    assert "contentExcerpt" not in json.dumps(row)
    for pointer in row["source_reads"]:
        saved = research_observations(
            parent,
            child,
            part_ids=[pointer["part_id"]],
            result_path=pointer["result_path"],
        )["observations"][0]
        assert saved["result"]["url"] == pointer["url"]
    assert (parent, child) == original


@pytest.mark.parametrize(
    "invalid", ["failed", "private", "search", "empty", "request_only"]
)
def test_case_source_pointer_requires_a_successful_document_read(
    compact_run: tuple[list[dict], list[dict]], invalid: str
) -> None:
    parent, child = compact_run
    read = observation(
        "wayfinder_core_web_fetch",
        {"results": [{"url": "https://example.test", "contentExcerpt": "text"}]},
        10,
    )
    part = read["parts"][0]
    part["id"] = "e10"
    if invalid == "failed":
        part["state"]["output"] = '{"ok":false}'
    elif invalid == "private":
        part["tool"] = "wayfinder_core_get_wallets"
    elif invalid == "search":
        part["tool"] = "wayfinder_core_web_search"
    elif invalid == "empty":
        part["state"]["output"] = (
            '{"ok":true,"result":{"results":[{"url":"https://example.test"}]}}'
        )
    else:
        part["state"]["input"] = {"urls": ["https://example.test"]}
        part["state"]["output"] = (
            '{"ok":true,"result":{"results":[{"url":"https://other.test","contentExcerpt":"text"}]}}'
        )
    child.append(read)
    row = research_notebook(parent, child, entities=["network"])["cases"][0]
    assert row["source_reads"] == []


@pytest.mark.parametrize(
    "action,field,slug_key",
    [("get_event", "event", "slug"), ("get_market", "market", "eventSlug")],
)
def test_prediction_case_links_saved_rules(
    compact_run: tuple[list[dict], list[dict]], action: str, field: str, slug_key: str
) -> None:
    parent, child = compact_run
    raw = deepcopy(child[0]["parts"][0]["state"]["input"]["checkpoint"])
    raw["research_cases"][0]["sources"] = ["https://polymarket.com/event/example"]
    child = [receipt(raw, 1, session="worker", agent="thesis-researcher")]
    child.append(
        observation(
            "wayfinder_polymarket_read",
            {
                "action": action,
                field: {
                    slug_key: "example",
                    "description": "Full calendar, including time before entry",
                },
            },
            10,
        )
    )
    child[-1]["parts"][0]["id"] = "e10"
    row = research_notebook(parent, child, entities=["network"])["cases"][0]
    assert row["source_reads"] == [
        {
            "url": "https://polymarket.com/event/example",
            "part_id": "e10",
            "result_path": [field],
        }
    ]


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


@pytest.fixture
def revised_notebook_run(
    compact_run: tuple[list[dict], list[dict]], discovery: dict
) -> tuple[list[dict], list[dict]]:
    parent, child = compact_run
    research = deepcopy(child[0]["parts"][0]["state"]["input"]["checkpoint"])
    research["research_cases"][0]["support"] = "Updated worker observation"
    child.append(receipt(research, 3, session="worker", agent="thesis-researcher"))
    research["research_cases"][0]["support"] = "Independent contrary observation"
    child.append(receipt(research, 4, session="other", agent="thesis-researcher"))
    update = deepcopy(parent[0]["parts"][0]["state"]["input"]["checkpoint"])
    # Deliberately retain the original research reference: a later worker case
    # must be visible, not silently substituted into the parent's decision.
    update["decisions"][0]["reason"] = "Latest parent judgment"
    parent.append(receipt(update, 5))
    parent.append(
        receipt(
            {"schema_version": 5, "stage": "discovery", "discoveries": [discovery]},
            6,
        )
    )
    return parent, child


def test_current_cases_keep_latest_parent_and_each_research_author(
    revised_notebook_run: tuple[list[dict], list[dict]],
) -> None:
    parent, child = revised_notebook_run
    original = deepcopy((parent, child))
    result = research_notebook(parent, child, entities=["network"], history=False)
    row = result["cases"][0]
    assert result["history"] is False
    assert row["record_count"] == 6
    assert row["visible_record_count"] == 4
    assert row["history_available"] is True
    assert row["next_offset"] is None
    assert [r["checkpoint_id"] for r in row["records"]] == ["t5", "t6", "t4", "t3"]
    assert [r["current_assessment"] for r in row["records"]] == [
        True,
        False,
        False,
        False,
    ]
    assert row["records"][0]["case"]["support"] == "Growing usage"
    assert row["records"][0]["case"]["reason"] == "Latest parent judgment"
    assert row["records"][2]["case"]["support"] == "Independent contrary observation"
    assert row["records"][3]["case"]["support"] == "Updated worker observation"
    assert result["evidence_verified"] is False
    assert (parent, child) == original


def test_current_case_pagination_and_history_are_separate(
    revised_notebook_run: tuple[list[dict], list[dict]],
) -> None:
    parent, child = revised_notebook_run
    pages = [
        research_notebook(
            parent, child, entities=["network"], history=False, limit=2, offset=offset
        )["cases"][0]
        for offset in (0, 2)
    ]
    assert [page["next_offset"] for page in pages] == [2, None]
    assert [r["checkpoint_id"] for page in pages for r in page["records"]] == [
        "t5",
        "t6",
        "t4",
        "t3",
    ]
    full = research_notebook(parent, child, entities=["network"], history=True)
    assert full["history"] is True
    assert [r["checkpoint_id"] for r in full["cases"][0]["records"]] == [
        "t1",
        "t2",
        "t3",
        "t4",
        "t5",
        "t6",
    ]
    # Existing SDK callers still get the historical view unless they opt in.
    assert research_notebook(parent, child, entities=["network"]) == full


def test_current_case_projection_preserves_metadata_and_unassessed_inventory(
    revised_notebook_run: tuple[list[dict], list[dict]],
) -> None:
    parent, child = revised_notebook_run
    result = research_notebook(
        [], child, entities=["NETWORK", "missing"], fields=["support"], history=False
    )
    row = result["cases"][0]
    assert result["missing"] == ["missing"]
    assert row["decision"] is None
    assert [r["session_id"] for r in row["records"]] == ["other", "worker"]
    assert all(not r["current_assessment"] for r in row["records"])
    assert all(r["case"]["observed_identifiers"] for r in row["records"])
    assert all(r["case"]["case_basis"] == "economic" for r in row["records"])
    discovery_only = research_notebook(
        [parent[-1]], [], entities=["network"], history=False
    )["cases"][0]
    assert discovery_only["ranked"] is False
    assert discovery_only["history_available"] is False
    assert discovery_only["records"][0]["checkpoint_id"] == "t6"


def test_current_view_does_not_change_inventory_or_publication(
    revised_notebook_run: tuple[list[dict], list[dict]],
) -> None:
    parent, child = revised_notebook_run
    before = draft_context(parent, child)
    assert research_notebook(parent, child, history=False) == research_notebook(
        parent, child
    )
    research_notebook(parent, child, entities=["network"], history=False)
    assert draft_context(parent, child) == before


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


@pytest.fixture
def saved_section_messages() -> list[dict[str, Any]]:
    result = {
        "items": [
            {"slug": "venue-v1", "total30d": 0, "verified": False, "change": None},
            {"slug": "venue-v2", "total30d": 1200},
        ],
        "0": "string key, not an array index",
    }
    return [
        {
            "parts": [
                {
                    "id": key,
                    "tool": tool,
                    "state": {
                        "status": "completed",
                        "time": {"end": 100},
                        "input": {"dataType": "dailyFees", "secret": "private"},
                        "output": json.dumps({"ok": True, "result": result}),
                    },
                }
                for key, tool in [
                    ("read", "wayfinder_research_defillama_free"),
                    ("private", "wallets"),
                ]
            ]
        }
    ]


@pytest.mark.parametrize(
    "path,expected",
    [
        (
            ["items", 0],
            {"slug": "venue-v1", "total30d": 0, "verified": False, "change": None},
        ),
        (["items", 0, "total30d"], 0),
        (["items", 0, "verified"], False),
        (["items", 0, "change"], None),
        (["0"], "string key, not an array index"),
    ],
)
def test_saved_evidence_sections_preserve_exact_values_and_provenance(
    saved_section_messages: list[dict[str, Any]], path: list[str | int], expected: Any
) -> None:
    before = deepcopy(saved_section_messages)
    full = research_observations(saved_section_messages, [], part_ids=["read"])
    report = research_observations(
        saved_section_messages, [], part_ids=["read", "read"], result_path=path
    )
    assert len(report["observations"]) == 1
    row = report["observations"][0]
    assert row["result"] == expected
    assert row["result_path"] == path
    assert row["partial_result"] is True
    for key in ("part_id", "tool", "completed_at_ms", "request_summary"):
        assert row[key] == full["observations"][0][key]
    assert not report["unavailable_result_paths"]
    assert report["evidence_verified"] is False
    assert "omitted fields" in report["note"]
    assert "secret" not in json.dumps(report)
    assert saved_section_messages == before
    assert research_observations(saved_section_messages, [], part_ids=["read"]) == full


@pytest.mark.parametrize(
    "path,navigation",
    [
        (
            ["absent"],
            {"resolved_path": [], "available_keys": ["items", "0"], "key_count": 2},
        ),
        (["items", 5], {"resolved_path": ["items"], "item_count": 2}),
        (["items", "0"], {"resolved_path": ["items"], "item_count": 2}),
        (["items", 0, "total30d", "x"], {"resolved_path": ["items", 0, "total30d"]}),
        (
            ["state", "input", "secret"],
            {"resolved_path": [], "available_keys": ["items", "0"], "key_count": 2},
        ),
    ],
)
def test_missing_sections_are_distinct_from_unavailable_observations(
    saved_section_messages: list[dict[str, Any]],
    path: list[str | int],
    navigation: dict[str, Any],
) -> None:
    before = deepcopy(saved_section_messages)
    report = research_observations(
        saved_section_messages,
        [],
        part_ids=["read", "private", "unknown"],
        result_path=path,
    )
    assert report["observations"] == []
    assert report["unavailable_part_ids"] == ["private", "unknown"]
    assert report["unavailable_result_paths"] == [
        {"part_id": "read", "result_path": path, **navigation}
    ]
    assert saved_section_messages == before


def test_missing_section_navigation_is_bounded_and_does_not_copy_values(
    saved_section_messages: list[dict[str, Any]],
) -> None:
    result = {"x" * 201: "not a usable path key"}
    result.update({f"field-{index}": "do not include values" for index in range(40)})
    saved_section_messages[0]["parts"][0]["state"]["output"] = json.dumps(
        {"ok": True, "result": result}
    )
    report = research_observations(
        saved_section_messages,
        [],
        part_ids=["read", "private"],
        result_path=["missing"],
    )
    assert report["observations"] == []
    assert report["unavailable_part_ids"] == ["private"]
    assert report["unavailable_result_paths"] == [
        {
            "part_id": "read",
            "result_path": ["missing"],
            "resolved_path": [],
            "available_keys": [f"field-{index}" for index in range(25)],
            "key_count": 41,
        }
    ]
    assert "do not include values" not in json.dumps(report)
    assert "not a usable path key" not in json.dumps(report)


@pytest.mark.parametrize(
    "path", [[], ["items", -1], ["items", True], [1.5], [{}], "items", ["x"] * 13]
)
def test_saved_evidence_section_paths_are_bounded(path: Any) -> None:
    with pytest.raises(ValueError, match="result_path"):
        research_observations([], [], part_ids=["read"], result_path=path)


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


@pytest.mark.parametrize("status", [None, "assessed", "out_of_scope", "needs_evidence"])
def test_ranked_alias_shares_assessment_but_retains_original_dissent(
    compact_run: tuple[list[dict], list[dict]], status: str | None
) -> None:
    parent, child = compact_run
    original = deepcopy(child[0]["parts"][0]["state"]["input"]["checkpoint"])
    alias = original["research_cases"][0]
    alias.update(
        entity="network-alias", counterevidence="Different contrary observation"
    )
    original["handoff"]["case_entities"] = ["network-alias"]
    child.append(
        receipt(original, 1, session="other-worker", agent="thesis-researcher")
    )
    saved = deepcopy(child)
    if status:
        parent.append(
            receipt(
                {
                    "schema_version": 5,
                    "stage": "judged",
                    "construction": {"mode": "directional"},
                    "discovery_dispositions": [
                        {
                            "entities": ["network-alias"],
                            "status": status,
                            "candidate_entity": "network"
                            if status == "assessed"
                            else None,
                            "reason": "Same underlying; differing observations retained",
                        }
                    ],
                },
                3,
            )
        )
    report = assessment_report(parent, child)
    assert bool(report["errors"]) is (status != "assessed")
    assert report["assessed_entities"] == 1
    notebook = research_notebook(
        parent, child, entities=["network-alias"], history=False
    )
    row = notebook["cases"][0]
    assert (
        row["records"][0]["case"]["counterevidence"] == "Different contrary observation"
    )
    if status == "assessed":
        assert row["disposition"]["candidate_entity"] == "network"
        assert not report["missing_entities"]
    assert child == saved


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


def review(
    blocking: bool = True, *, scope: Literal["assessment", "draft"] | None = None
) -> dict[str, Any]:
    cp = ReviewCheckpoint(
        findings=[
            {
                "id": "carry",
                "entity": "network",
                "blocking": blocking,
                "issue": "Spot unchecked",
                "required_change": "Compare spot",
                **({"scope": scope} if scope is not None else {}),
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


@pytest.mark.parametrize("include_proposal", [False, True])
def test_large_review_ledger_is_paged_without_hiding_unresolved_findings(
    compact_run: tuple[list[dict], list[dict]], include_proposal: bool
) -> None:
    parent, child = compact_run
    message = review()
    checkpoint = ReviewCheckpoint.model_validate(
        {
            "findings": [
                {
                    "id": f"finding-{index:02}",
                    "entity": "network",
                    "blocking": index == 1,
                    "issue": "Unverified observation. " * 70,
                    "required_change": "Compare exact saved evidence. " * 60,
                }
                for index in range(24)
            ]
        }
    )
    state = message["parts"][0]["state"]
    state["input"]["checkpoint"] = checkpoint.model_dump()
    state["output"] = json.dumps(
        {
            "ok": True,
            "result": {
                "sha256": hashlib.sha256(checkpoint.receipt_json().encode()).hexdigest()
            },
        }
    )
    child.append(message)
    parent.append(
        receipt(
            {
                "schema_version": 5,
                "stage": "discovery",
                "review_resolutions": [
                    {
                        "review_session_id": "reviewer",
                        "finding_id": f"finding-{index:02}",
                        "action": "accepted",
                        "reason": "Disclosed nonblocking uncertainty",
                    }
                    for index in range(2, 24)
                ],
            },
            5,
        )
    )
    original = deepcopy((parent, child))
    proposal, evidence, reference = draft_context(parent, child)
    full = evidence["review"]["findings"]
    assert len(json.dumps(full).encode()) > 48_000
    seen = []
    offset = 0
    while offset is not None:
        page = draft_status(
            parent, child, include_proposal=include_proposal, offset=offset, limit=1
        )
        assert len(json.dumps(page).encode()) < 48_000
        assert not page["ready"] and page["proposal_ref"] is None
        assert any("finding-01" in error for error in page["errors"])
        rows = page["review"]["findings"]
        paging = page["review"]["findings_page"]
        assert paging["total"] == 24
        assert paging["unresolved"] == 2
        assert paging["unresolved_blocking"] == 1
        seen.extend(rows)
        offset = paging["next_offset"]
    assert [row["id"] for row in seen[:2]] == ["finding-01", "finding-00"]
    assert sorted(seen, key=lambda row: row["id"]) == full
    assert draft_context(parent, child) == (proposal, evidence, reference)
    assert (parent, child) == original


@pytest.mark.parametrize(
    "scope,blocking,action,refs,updated_at,artifact,valid",
    [
        (None, True, "accepted", [], [], "assessment", False),
        (None, False, "accepted", [], [], "assessment", True),
        (None, True, "changed", [], [], "assessment", False),
        (None, True, "changed", ["public"], [], "assessment", False),
        (None, True, "changed", ["public"], [2], "assessment", False),
        (None, True, "changed", ["public"], [4], "assessment", True),
        (None, True, "changed", ["public"], [6], "assessment", False),
        (None, True, "changed", ["public"], [4, 6], "assessment", True),
        (None, True, "evidence", ["invented"], [], "assessment", False),
        (None, True, "evidence", ["public"], [], "assessment", True),
        (None, True, "removed", [], [], "assessment", False),
        ("assessment", True, "changed", ["public"], [4], "draft", False),
        ("draft", True, "changed", ["public"], [4], "draft", True),
        ("draft", True, "changed", ["public"], [], "draft", False),
        ("draft", True, "changed", ["public"], [2], "draft", False),
        ("draft", True, "changed", ["public"], [6], "draft", False),
        ("draft", True, "changed", ["public"], [4], "assessment", False),
        ("draft", True, "changed", [], [4], "draft", False),
        ("draft", True, "changed", ["invented"], [4], "draft", False),
        ("draft", True, "accepted", [], [4], "draft", False),
    ],
)
def test_findings_require_substantive_resolution(
    compact_run: tuple[list[dict], list[dict]],
    scope: Literal["assessment", "draft"] | None,
    blocking: bool,
    action: str,
    refs: list[str],
    updated_at: list[int],
    artifact: str,
    valid: bool,
) -> None:
    parent, child = compact_run
    original_child = deepcopy(child)
    for timestamp in updated_at:
        if artifact == "draft":
            update = {
                "schema_version": 6,
                "stage": "draft",
                "draft": {"remove_components": ["removed-leg"]},
            }
        else:
            update = deepcopy(parent[0]["parts"][0]["state"]["input"]["checkpoint"])
            update["decisions"][0]["reason"] = "Corrected implementation comparison"
        parent.append(receipt(update, timestamp))
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
        parent, [*child, review(blocking, scope=scope)], records, {"NETWORK-USDC"}
    )
    assert (not report["errors"]) == valid
    assert child == original_child
    if valid and scope == "draft":
        # Correct bookkeeping still cannot substitute for current native sign-off.
        report = review_report(
            parent,
            [*child, review(blocking, scope=scope)],
            records,
            {"NETWORK-USDC"},
            revision="corrected-draft",
        )
        assert any("sign-off" in error for error in report["errors"])
        report = review_report(
            parent,
            [*child, review(blocking, scope=scope), signoff("corrected-draft", 7)],
            records,
            {"NETWORK-USDC"},
            revision="corrected-draft",
        )
        assert not report["errors"]


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
