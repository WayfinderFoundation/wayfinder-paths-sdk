"""Transcript-backed handoff accounting, not verification of economic judgments."""

import hashlib
import json
from typing import Any

from pydantic import ValidationError

from wayfinder_paths.core.theses.checkpoints import ResearchCheckpoint

CHECKPOINT_TOOL = "wayfinder_research_thesis_checkpoint"


def checkpoints(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records = []
    for message in messages:
        for part in message.get("parts", []):
            state = part.get("state", {})
            if (
                part.get("tool") != CHECKPOINT_TOOL
                or state.get("status") != "completed"
            ):
                continue
            try:
                checkpoint = ResearchCheckpoint.model_validate(
                    state.get("input", {}).get("checkpoint")
                )
                output = json.loads(state.get("output", ""))
            except (ValidationError, ValueError, TypeError):
                continue
            if not isinstance(output, dict) or output.get("ok") is not True:
                continue
            receipt = output.get("result")
            digests = {
                hashlib.sha256(checkpoint.model_dump_json().encode()).hexdigest()
            }
            # Read v2 receipts issued before compact dispositions were added.
            if "discovery_dispositions" not in state["input"]["checkpoint"]:
                digests.add(
                    hashlib.sha256(
                        checkpoint.model_dump_json(
                            exclude={"discovery_dispositions"}
                        ).encode()
                    ).hexdigest()
                )
            if not isinstance(receipt, dict) or receipt.get("sha256") not in digests:
                continue
            records.append(
                {
                    "id": part.get("id"),
                    "completed_at_ms": state.get("time", {}).get("end"),
                    "checkpoint": checkpoint.model_dump(),
                }
            )
    return sorted(records, key=lambda r: r["completed_at_ms"] or 0)


def assessment_report(
    parent_messages: list[dict[str, Any]],
    child_messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Every admitted discovery survives to judgment, including known implementations.

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
                or part["tool"] == CHECKPOINT_TOOL
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
        if not any(
            r["checkpoint"]["stage"] == "discovery"
            and r["checkpoint"]["schema_version"] == 2
            for r in records
        ):
            errors.append(
                f"Research child {session_id} did not record its discovery inventory"
            )
    judged = next(
        (
            r["checkpoint"]
            for r in reversed(parent)
            if r["checkpoint"]["stage"] == "judged"
            and r["checkpoint"]["schema_version"] == 2
        ),
        None,
    )
    cases = judged["candidates"] if judged else []
    dispositions = {
        entity.casefold(): d
        for d in (judged or {}).get("discovery_dispositions", [])
        for entity in d["entities"]
    }
    if judged is None:
        errors.append(
            "Record a v2 judged checkpoint with the complete candidate ledger"
        )
    discoveries = [
        d
        for r in [*parent, *children]
        for d in [*r["checkpoint"]["discoveries"], *r["checkpoint"]["candidates"]]
    ]
    # Union all snapshots; a later shorter/empty ledger cannot erase discoveries.
    missing = set()
    unobserved_instruments = set()
    deferred: set[str] = set()
    excluded: set[str] = set()
    case_by_entity = {c["entity"].casefold(): c for c in cases}
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
            missing.add(discovery["entity"])
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
    for case in cases:
        for check in case["implementation_checks"]:
            if check["status"] in {"viable", "rejected"} and not any(
                check["instrument_id"].casefold() in output for output in outputs
            ):
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
        "unobserved_instrument_claims": sorted(unobserved_instruments),
        "deferred_discoveries": sorted(deferred),
        "out_of_scope_discoveries": sorted(excluded),
        "unresolved_entities": [
            c["entity"] for c in cases if c["decision"] == "NEEDS_EVIDENCE"
        ],
        "semantic_review_required": True,
    }
