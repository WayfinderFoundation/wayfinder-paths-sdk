"""Transcript-backed handoff accounting, not verification of economic judgments."""

import hashlib
import json
from typing import Any

from pydantic import ValidationError
from pydantic_core import to_json

from wayfinder_paths.core.theses.checkpoints import (
    CandidateCase,
    DiscoveryCheckpoint,
    ResearchCheckpoint,
)

CHECKPOINT_TOOL = "wayfinder_research_thesis_checkpoint"
DISCOVERY_TOOL = "wayfinder_research_thesis_discovery"
CHECKPOINT_TOOLS = {CHECKPOINT_TOOL, DISCOVERY_TOOL}


def checkpoints(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records = []
    for message in messages:
        for part in message.get("parts", []):
            state = part.get("state", {})
            if (
                part.get("tool") not in CHECKPOINT_TOOLS
                or state.get("status") != "completed"
            ):
                continue
            try:
                payload = state.get("input", {}).get("checkpoint")
                if part.get("tool") == DISCOVERY_TOOL:
                    payload = DiscoveryCheckpoint.model_validate(payload).model_dump()
                checkpoint = ResearchCheckpoint.model_validate(payload)
                output = json.loads(state.get("output", ""))
            except (ValidationError, ValueError, TypeError):
                continue
            if not isinstance(output, dict) or output.get("ok") is not True:
                continue
            receipt = output.get("result")
            digests = {
                hashlib.sha256(checkpoint.model_dump_json().encode()).hexdigest(),
                hashlib.sha256(checkpoint.receipt_json().encode()).hexdigest(),
            }
            # Read v2 receipts issued before compact dispositions were added.
            if "discovery_dispositions" not in state["input"]["checkpoint"]:
                digests.add(
                    hashlib.sha256(
                        checkpoint.model_dump_json(
                            exclude={"discovery_dispositions", "construction", "draft"}
                        ).encode()
                    ).hexdigest()
                )
            if checkpoint.schema_version < 3:
                # Preserve byte ordering of receipts issued before ResearchCase
                # was extracted. Never use raw, unvalidated input as a digest.
                legacy = checkpoint.model_dump(
                    mode="json", exclude={"research_cases", "construction", "draft"}
                )
                fields = (
                    "entity",
                    "name",
                    "mechanism",
                    "observed_identifiers",
                    "sources",
                    "instruments",
                    "effect_order",
                    "value_capture",
                    "support",
                    "counterevidence",
                    "closest_alternative",
                    "decision",
                    "reason",
                    "gaps",
                    "decision_basis",
                    "implementation_checks",
                )
                legacy["candidates"] = [
                    {key: case[key] for key in fields} for case in legacy["candidates"]
                ]
                digests.add(hashlib.sha256(to_json(legacy)).hexdigest())
                if "discovery_dispositions" not in state["input"]["checkpoint"]:
                    legacy.pop("discovery_dispositions")
                    digests.add(hashlib.sha256(to_json(legacy)).hexdigest())
            if not isinstance(receipt, dict) or receipt.get("sha256") not in digests:
                continue
            records.append(
                {
                    "id": part.get("id"),
                    "session_id": message.get("info", {}).get("sessionID"),
                    "completed_at_ms": state.get("time", {}).get("end"),
                    "checkpoint": checkpoint.model_dump(),
                }
            )
    return sorted(records, key=lambda r: r["completed_at_ms"] or 0)


def assessment_report(
    parent_messages: list[dict[str, Any]],
    child_messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Preserve discoveries; require assessments for ranked cases and known implementations.

    Raw search mentions still require semantic triage. This gate cannot certify
    discovery recall, causal reasoning, or the truth of an agent's rejection reason.
    Child checkpoints can add discoveries, never judge or authorize a portfolio.
    """
    parent = checkpoints(parent_messages)
    children = checkpoints(child_messages)
    outputs = []
    for message in [*parent_messages, *child_messages]:
        for part in message.get("parts", []):
            state = part.get("state", {})
            if (
                not part.get("tool", "").startswith("wayfinder_")
                or part["tool"] in CHECKPOINT_TOOLS
                or state.get("status") != "completed"
            ):
                continue
            try:
                output = json.loads(state.get("output", ""))
            except (ValueError, TypeError):
                continue
            if isinstance(output, dict) and output.get("ok") is True:
                outputs.append(
                    json.dumps(output.get("result"), ensure_ascii=False).casefold()
                )
    errors = []
    if any(r["checkpoint"]["stage"] != "discovery" for r in children):
        errors.append("Researchers may record discoveries, not parent judgments")
    research_sessions = {
        m["info"]["sessionID"]
        for m in child_messages
        if m.get("info", {}).get("agent") == "thesis-researcher"
        and m["info"].get("sessionID")
    }
    for session_id in research_sessions:
        records = checkpoints(
            [
                m
                for m in child_messages
                if m.get("info", {}).get("sessionID") == session_id
            ]
        )
        if not any(r["checkpoint"]["stage"] == "discovery" for r in records):
            errors.append(
                f"Research child {session_id} did not record its discovery inventory"
            )
    judgments = [
        r["checkpoint"]
        for r in parent
        if r["checkpoint"]["stage"] == "judged"
        and r["checkpoint"]["schema_version"] >= 2
    ]
    # Checkpoints are an append-only transcript. Corrections upsert by key;
    # omitting an unchanged case/link cannot erase its earlier assessment.
    case_by_entity = {
        case["entity"].casefold(): case for j in judgments for case in j["candidates"]
    }
    cases = list(case_by_entity.values())
    dispositions = {
        entity.casefold(): d
        for j in judgments
        for d in j["discovery_dispositions"]
        for entity in d["entities"]
    }
    if not cases:
        errors.append("Record a judged checkpoint with the complete candidate ledger")
    discoveries = [
        d
        for r in [*parent, *children]
        for d in [
            *r["checkpoint"]["discoveries"],
            *r["checkpoint"]["research_cases"],
            *r["checkpoint"]["candidates"],
        ]
    ]
    # New runs distinguish the broad inbox from ranked comparisons. Legacy
    # traces retain their original accounting semantics when replayed.
    ranked = {
        d["entity"].casefold()
        for r in [*parent, *children]
        for d in [*r["checkpoint"]["research_cases"], *r["checkpoint"]["candidates"]]
    }
    required = (
        ranked
        if any(r["checkpoint"]["schema_version"] >= 3 for r in parent)
        else {d["entity"].casefold() for d in discoveries}
    )
    # Union all snapshots; a later shorter/empty ledger cannot erase discoveries.
    missing = set()
    unobserved_instruments = set()
    deferred: set[str] = set()
    excluded: set[str] = set()
    unassessed: set[str] = set()
    for disposition in dispositions.values():
        if (
            disposition["status"] == "assessed"
            and disposition["candidate_entity"].casefold() not in case_by_entity
        ):
            errors.append(
                f"Discovery links to missing candidate {disposition['candidate_entity']}"
            )
    for discovery in discoveries:
        key = discovery["entity"].casefold()
        disposition = dispositions.get(key)
        if disposition and disposition["status"] != "assessed":
            if key in ranked:
                errors.append(f"{key}: ranked cases require a candidate assessment")
            if key in case_by_entity:
                errors.append(
                    f"{key}: discovery disposition conflicts with its candidate assessment"
                )
            if disposition["status"] == "needs_evidence":
                deferred.add(key)
            else:
                excluded.add(key)
            continue
        case = case_by_entity.get(
            disposition["candidate_entity"].casefold() if disposition else key
        )
        if case is None:
            if key in required:
                missing.add(discovery["entity"])
            else:
                unassessed.add(discovery["entity"])
            continue
        # An inbox is untrusted: model-added namespaces/guessed IDs must not
        # become mandatory implementation identities merely by being recorded.
        known = {
            i
            for i in discovery["instruments"]
            if any(i.casefold() in output for output in outputs)
        }
        unobserved_instruments.update(set(discovery["instruments"]) - known)
        if case["decision_basis"] == "implementation" and case["decision"] in {
            "REJECT",
            "ALTERNATIVE",
        }:
            compared = {
                c["instrument_id"]
                for c in case["implementation_checks"]
                if c["status"] != "unverified"
            }
            unchecked = known - compared
            if unchecked:
                errors.append(
                    f"{case['entity']}: implementation rejection did not compare "
                    + ", ".join(sorted(unchecked))
                )
    if missing:
        errors.append("Discoveries missing assessment: " + ", ".join(sorted(missing)))
    unobserved_comparisons = []
    for case in cases:
        for check in case["implementation_checks"]:
            if check["status"] in {"viable", "rejected"} and not any(
                check["instrument_id"].casefold() in output for output in outputs
            ):
                unobserved_comparisons.append(
                    {"entity": case["entity"], "instrument_id": check["instrument_id"]}
                )
                if case["decision_basis"] == "implementation" and case["decision"] in {
                    "REJECT",
                    "ALTERNATIVE",
                }:
                    errors.append(
                        f"{case['entity']}: compared implementation ID was not observed in successful public reads"
                    )
    return {
        "errors": list(dict.fromkeys(errors)),
        "discovery_keys": len({d["entity"].casefold() for d in discoveries}),
        "assessed_entities": len(cases),
        "kept_instruments": sorted(
            {
                instrument
                for case in cases
                if case["decision"] == "KEEP"
                for instrument in case["instruments"]
            }
        ),
        "missing_entities": sorted(missing),
        "unassessed_discoveries": sorted(unassessed),
        "unobserved_instrument_claims": sorted(unobserved_instruments),
        "unobserved_comparison_claims": unobserved_comparisons,
        "deferred_discoveries": sorted(deferred),
        "out_of_scope_discoveries": sorted(excluded),
        "unresolved_entities": [
            c["entity"] for c in cases if c["decision"] == "NEEDS_EVIDENCE"
        ],
        "semantic_review_required": True,
    }


def research_notebook(
    parent_messages: list[dict[str, Any]],
    child_messages: list[dict[str, Any]],
    *,
    entities: list[str] | None = None,
    fields: list[str] | None = None,
    offset: int = 0,
    limit: int = 25,
) -> dict[str, Any]:
    """A compact index or selected original cases; never an LLM re-summary."""
    if (
        offset < 0
        or not 1 <= limit <= 100
        or (entities is not None and len(entities) > 10)
    ):
        raise ValueError("Use offset>=0, limit 1..100 and at most ten entity keys")
    if fields is not None and (
        not fields or set(fields) - CandidateCase.model_fields.keys()
    ):
        raise ValueError("fields must name existing research/candidate fields")
    records = sorted(
        [*checkpoints(parent_messages), *checkpoints(child_messages)],
        key=lambda r: r["completed_at_ms"] or 0,
    )
    dispositions = {
        key.casefold(): disposition
        for record in checkpoints(parent_messages)
        if record["checkpoint"]["stage"] == "judged"
        for disposition in record["checkpoint"]["discovery_dispositions"]
        for key in disposition["entities"]
    }
    rows: dict[str, dict[str, Any]] = {}
    for record in records:
        checkpoint = record["checkpoint"]
        for kind in ("discoveries", "research_cases", "candidates"):
            for case in checkpoint[kind]:
                key = case["entity"].casefold()
                row = rows.setdefault(
                    key,
                    {
                        "entity": key,
                        "name": case["name"],
                        "instruments": [],
                        "ranked": False,
                        "decision": None,
                        "records": [],
                    },
                )
                row["instruments"] = sorted(
                    set(row["instruments"]) | set(case["instruments"])
                )
                row["ranked"] |= kind != "discoveries"
                if kind == "candidates":
                    row["decision"] = case["decision"]
                row["records"].append(
                    {
                        "checkpoint_id": record["id"],
                        "session_id": record["session_id"],
                        "kind": kind,
                        "case": case,
                    }
                )
    if entities is not None:
        requested = list(dict.fromkeys(e.casefold() for e in entities))
        for key in requested:
            if key not in rows:
                continue
            row = rows[key]
            if key in dispositions:
                row["disposition"] = dispositions[key]
            count = len(row["records"])
            row["record_count"] = count
            row["next_offset"] = offset + limit if offset + limit < count else None
            row["records"] = row["records"][offset : offset + limit]
            if fields is not None:
                for record in row["records"]:
                    record["case"] = {
                        k: v for k, v in record["case"].items() if k in fields
                    }
        return {
            "cases": [rows[key] for key in requested if key in rows],
            "missing": [key for key in requested if key not in rows],
            "evidence_verified": False,
        }
    keys = sorted(rows)
    return {
        "total": len(keys),
        "items": [
            {k: v for k, v in rows[key].items() if k != "records"}
            for key in keys[offset : offset + limit]
        ],
        "next_offset": offset + limit if offset + limit < len(keys) else None,
        "discovery_dispositions": [
            {**dispositions[key], "entities": [key]}
            for key in keys[offset : offset + limit]
            if key in dispositions
        ],
        "evidence_verified": False,
    }


if __name__ == "__main__":
    # Native OpenCode tool passes only its verified session tree over stdin.
    import sys

    request = json.load(sys.stdin)
    view = request.pop("view", "cases")
    if view in {"status", "draft"}:
        from wayfinder_paths.core.theses.draft import draft_status

        result = draft_status(
            request["parent_messages"],
            request["child_messages"],
            include_proposal=view == "draft",
        )
    elif view == "cases":
        result = research_notebook(**request)
    else:
        raise ValueError("Unknown notebook view")
    print(json.dumps(result))
