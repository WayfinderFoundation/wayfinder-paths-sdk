"""Transcript-backed handoff accounting, not verification of economic judgments."""

import hashlib
import json
from copy import deepcopy
from typing import Any

from pydantic import ValidationError
from pydantic_core import to_json

from wayfinder_paths.core.theses.checkpoints import (
    V5_FIELDS,
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
                output = json.loads(state.get("output", ""))
                if not isinstance(output, dict) or output.get("ok") is not True:
                    continue
                receipt = output.get("result")
                if not isinstance(receipt, dict):
                    continue
                payload = state.get("input", {}).get("checkpoint")
                if part.get("tool") == DISCOVERY_TOOL:
                    if isinstance(payload, dict):
                        # Older omitted-version worker inputs meant v3. New
                        # receipts record the effective default so both replay.
                        payload = {
                            "schema_version": receipt.get("schema_version", 3),
                            **payload,
                        }
                    payload = DiscoveryCheckpoint.model_validate(payload).model_dump()
                checkpoint = ResearchCheckpoint.model_validate(payload)
            except (ValidationError, ValueError, TypeError):
                continue
            if (
                receipt.get("schema_version", checkpoint.schema_version)
                != checkpoint.schema_version
            ):
                continue
            digests = {
                hashlib.sha256(checkpoint.model_dump_json().encode()).hexdigest(),
                hashlib.sha256(checkpoint.receipt_json().encode()).hexdigest(),
            }
            # Read v2 receipts issued before compact dispositions were added.
            if "discovery_dispositions" not in state["input"]["checkpoint"]:
                digests.add(
                    hashlib.sha256(
                        checkpoint.model_dump_json(
                            exclude=V5_FIELDS
                            | {"discovery_dispositions", "construction", "draft"}
                        ).encode()
                    ).hexdigest()
                )
            if checkpoint.schema_version < 3:
                # Preserve byte ordering of receipts issued before ResearchCase
                # was extracted. Never use raw, unvalidated input as a digest.
                legacy = checkpoint.model_dump(
                    mode="json",
                    exclude=V5_FIELDS | {"research_cases", "construction", "draft"},
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
            if receipt.get("sha256") not in digests:
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


def projected_records(
    parent_messages: list[dict[str, Any]], child_messages: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Resolve compact decisions only against validated receipts in this session tree."""
    parent, children = checkpoints(parent_messages), checkpoints(child_messages)
    sources = {
        (r["session_id"], r["id"], c["entity"].casefold()): (c, r["completed_at_ms"])
        for r in [*parent, *children]
        for c in r["checkpoint"]["research_cases"]
    }
    errors_by_entity: dict[str, list[str]] = {}
    current_research: dict[str, tuple[tuple[str, str, str], dict[str, Any]]] = {}
    construction = None
    for record in parent:
        cp = record["checkpoint"]
        if cp["construction"] is not None:
            construction = cp["construction"]
        elif cp["schema_version"] >= 6:
            # The interpretation owns this state; don't make every incremental
            # judgment/draft copy it. Explicit changes are still checked by draft_context.
            cp["construction"] = construction
        for candidate in cp["candidates"]:
            errors_by_entity.pop(candidate["entity"].casefold(), None)
            current_research.pop(candidate["entity"].casefold(), None)
        for decision in cp["decisions"]:
            key = decision["entity"].casefold()
            ref = decision["research_ref"]
            source_key = (
                ref["session_id"],
                ref["checkpoint_id"],
                ref["entity"].casefold(),
            )
            source = sources.get(source_key)
            if source is None or (source[1] or 0) > (record["completed_at_ms"] or 0):
                errors_by_entity[key] = [
                    f"{decision['entity']}: research_ref is not an earlier saved case in this run"
                ]
                continue
            research = decision["updated_research"]
            if research is None:
                # A new verdict over the same source must not resurrect research
                # corrected in an earlier parent decision. New refs start fresh.
                previous = current_research.get(key)
                research = (
                    previous[1] if previous and previous[0] == source_key else source[0]
                )
            payload = {
                **deepcopy(research),
                **{
                    k: v
                    for k, v in decision.items()
                    if k not in {"research_ref", "updated_research"}
                },
            }
            try:
                # Reuse the existing judgment checks, including viable alternatives.
                validated = ResearchCheckpoint.model_validate(
                    {
                        "schema_version": 7 if cp["schema_version"] >= 7 else 5,
                        "stage": "judged",
                        "construction": cp["construction"],
                        "candidates": [payload],
                    }
                )
            except ValidationError as exc:
                errors_by_entity[key] = [
                    f"{decision['entity']}: {e['msg']}"
                    for e in exc.errors(include_input=False)
                ]
                continue
            errors_by_entity.pop(key, None)
            current_research[key] = (source_key, research)
            cp["candidates"].append(validated.candidates[0].model_dump())
            if ref["entity"].casefold() != decision["entity"].casefold():
                cp["discovery_dispositions"].append(
                    {
                        "entities": [ref["entity"]],
                        "status": "assessed",
                        "candidate_entity": decision["entity"],
                        "reason": "Explicit research reference",
                    }
                )
    return (
        parent,
        children,
        [error for errors in errors_by_entity.values() for error in errors],
    )


def assessment_report(
    parent_messages: list[dict[str, Any]],
    child_messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Preserve discoveries; require assessments for ranked cases and known implementations.

    Raw search mentions still require semantic triage. This gate cannot certify
    discovery recall, causal reasoning, or the truth of an agent's rejection reason.
    Child checkpoints can add discoveries, never judge or authorize a portfolio.
    """
    parent, children, errors = projected_records(parent_messages, child_messages)
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
    if any(r["checkpoint"]["stage"] != "discovery" for r in children):
        errors.append("Researchers may record discoveries, not parent judgments")
    research_sessions = {
        m["info"]["sessionID"]
        for m in child_messages
        if m.get("info", {}).get("agent") == "thesis-researcher"
        and m["info"].get("sessionID")
    }
    handoff_gaps = {
        g["session_id"]: g["reason"]
        for r in parent
        for g in r["checkpoint"]["handoff_gaps"]
    }
    for session_id in research_sessions:
        records = checkpoints(
            [
                m
                for m in child_messages
                if m.get("info", {}).get("sessionID") == session_id
            ]
        )
        if session_id not in handoff_gaps and not any(
            r["checkpoint"]["stage"] == "discovery" for r in records
        ):
            errors.append(
                f"Research child {session_id} did not record its discovery inventory"
            )
    incomplete_handoffs = []
    if any(r["checkpoint"]["schema_version"] >= 5 for r in parent):
        for session_id in research_sessions:
            records = [r for r in children if r["session_id"] == session_id]
            manifests = [
                r["checkpoint"]["handoff"]
                for r in records
                if r["checkpoint"]["handoff"]
            ]
            ranked_keys = {
                c["entity"].casefold()
                for r in records
                for c in r["checkpoint"]["research_cases"]
            }
            inventory = {
                c["entity"].casefold()
                for r in records
                for kind in ("discoveries", "research_cases")
                for c in r["checkpoint"][kind]
            }
            manifest = manifests[-1] if manifests else None
            mismatches = []
            if manifest is None:
                mismatches.append("missing handoff manifest")
            else:
                for field, expected in (
                    ("case_entities", ranked_keys),
                    ("unresolved_entities", inventory - ranked_keys),
                ):
                    reported = {k.casefold() for k in manifest[field]}
                    for label, keys in (
                        ("missing", expected - reported),
                        ("unexpected", reported - expected),
                    ):
                        if keys:
                            mismatches.append(
                                f"{field} {label}: {', '.join(sorted(keys))}"
                            )
            if mismatches:
                incomplete_handoffs.append(session_id)
                if session_id not in handoff_gaps:
                    errors.append(
                        f"Research child {session_id}: incomplete handoff "
                        f"({'; '.join(mismatches)}); resume once or record handoff_gaps explicitly"
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
    # Parent-promoted/assigned leads are deliberate, unlike a broad worker inbox.
    # Every one needs an outcome, even when a worker never promotes it to a case.
    assigned = {
        d["entity"].casefold()
        for r in parent
        if r["checkpoint"]["schema_version"] >= 6
        for d in r["checkpoint"]["discoveries"]
    }
    required = required | assigned
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
    kept_instruments = {
        instrument
        for case in cases
        if case["decision"] == "KEEP"
        for instrument in case["instruments"]
    }
    unobserved_comparisons = []
    for case in cases:
        for check in case["implementation_checks"]:
            if check["status"] not in {"viable", "rejected"}:
                continue
            if not any(
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
            elif case["decision"] == "KEEP" and check["status"] == "viable":
                kept_instruments.add(check["instrument_id"])
    return {
        "errors": list(dict.fromkeys(errors)),
        "discovery_keys": len({d["entity"].casefold() for d in discoveries}),
        "assessed_entities": len(cases),
        "kept_instruments": sorted(kept_instruments),
        "missing_entities": sorted(missing),
        "assigned_entities": sorted(assigned),
        "unassessed_discoveries": sorted(unassessed),
        "unobserved_instrument_claims": sorted(unobserved_instruments),
        "unobserved_comparison_claims": unobserved_comparisons,
        "deferred_discoveries": sorted(deferred),
        "out_of_scope_discoveries": sorted(excluded),
        "unresolved_entities": [
            c["entity"] for c in cases if c["decision"] == "NEEDS_EVIDENCE"
        ],
        "incomplete_handoffs": incomplete_handoffs,
        "handoff_gaps": handoff_gaps,
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
    history: bool = True,
) -> dict[str, Any]:
    """An inventory, current cases or their history; never an LLM re-summary."""
    from wayfinder_paths.core.theses.review import public_observations

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
    parent, children, errors = projected_records(parent_messages, child_messages)
    records = sorted(
        [*parent, *children],
        key=lambda r: r["completed_at_ms"] or 0,
    )
    dispositions = {
        key.casefold(): disposition
        for record in parent
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
                        "research_refs": [],
                    },
                )
                row["instruments"] = sorted(
                    set(row["instruments"]) | set(case["instruments"])
                )
                row["ranked"] |= kind != "discoveries"
                if kind == "candidates":
                    row["decision"] = case["decision"]
                if kind == "research_cases":
                    row["research_refs"].append(
                        {
                            "session_id": record["session_id"],
                            "checkpoint_id": record["id"],
                            "entity": case["entity"],
                        }
                    )
                row["records"].append(
                    {
                        "checkpoint_id": record["id"],
                        "session_id": record["session_id"],
                        "kind": kind,
                        "case": case,
                    }
                )
    if entities is not None:
        source_reads: dict[str, list[dict[str, Any]]] = {}
        for observation in public_observations(
            [*parent_messages, *child_messages], include_results=True
        ).values():
            result = observation["result"]
            if not isinstance(result, dict):
                continue
            tool = observation["tool"]
            pages: list[tuple[Any, list[str | int]]]
            if tool == "wayfinder_core_web_fetch":
                pages = [
                    (page.get("url"), ["results", index])
                    for index, page in enumerate(result.get("results", []))
                    if isinstance(page, dict)
                    and (
                        page.get("contentExcerpt")
                        or page.get("text")
                        or page.get("content")
                    )
                ]
            elif tool == "wayfinder_polymarket_read" and result.get("action") in {
                "get_event",
                "get_market",
            }:
                field = "event" if result["action"] == "get_event" else "market"
                page = result.get(field) or {}
                slug = page.get("slug" if field == "event" else "eventSlug")
                pages = (
                    [(f"https://polymarket.com/event/{slug}", [field])]
                    if slug and (page.get("description") or page.get("rules"))
                    else []
                )
            elif tool == "wayfinder_research_defillama_free" and isinstance(
                result.get("result"), (dict, list)
            ):
                # Match the returned URL exactly, including metric query params.
                # Keep the whole provider result: totals alone omit scope/periods.
                pages = [(result.get("url"), ["result"])]
            else:
                continue
            for url, path in pages:
                if isinstance(url, str) and url:
                    source_reads.setdefault(url, []).append(
                        {"part_id": observation["part_id"], "result_path": path}
                    )
        requested = list(dict.fromkeys(e.casefold() for e in entities))
        for key in requested:
            if key not in rows:
                continue
            row = rows[key]
            row["source_reads"] = [
                {"url": url, **read}
                for url in sorted(
                    {source for r in row["records"] for source in r["case"]["sources"]}
                )
                for read in source_reads.get(url, [])
            ]
            if key in dispositions:
                row["disposition"] = dispositions[key]
            current = next(
                (r for r in reversed(row["records"]) if r["kind"] == "candidates"),
                None,
            )
            for record in row["records"]:
                record["current_assessment"] = record is current
            count = len(row["records"])
            row["record_count"] = count
            if not history:
                # Keep each author's latest research/discovery, including dissent.
                # A newer worker case must not silently replace the research that
                # the current parent judgment actually referenced.
                latest = {(r["session_id"], r["kind"]): r for r in row["records"]}
                row["records"] = ([current] if current is not None else []) + [
                    r
                    for r in reversed(row["records"])
                    if r["kind"] != "candidates"
                    and latest[(r["session_id"], r["kind"])] is r
                ]
                row["visible_record_count"] = len(row["records"])
                row["history_available"] = len(row["records"]) < count
            row["next_offset"] = (
                offset + limit if offset + limit < len(row["records"]) else None
            )
            row["records"] = row["records"][offset : offset + limit]
            if fields is not None:
                for record in row["records"]:
                    record["case"] = {
                        k: v
                        for k, v in record["case"].items()
                        if k in fields
                        or k
                        in {
                            "case_basis",
                            "effect_order",
                            "decision",
                            "decision_basis",
                            "observed_identifiers",
                            "claims",
                            "comparison_refs",
                        }
                    }
        return {
            "errors": errors,
            "history": history,
            "cases": [rows[key] for key in requested if key in rows],
            "missing": [key for key in requested if key not in rows],
            "evidence_verified": False,
        }
    keys = sorted(rows)
    return {
        "errors": errors,
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


def research_observations(
    parent_messages: list[dict[str, Any]],
    child_messages: list[dict[str, Any]],
    *,
    part_ids: list[str],
    result_path: list[str | int] | None = None,
) -> dict[str, Any]:
    """Retrieve exact saved public results, never re-fetch or summarize them."""
    from wayfinder_paths.core.theses.review import public_observations

    if (
        not isinstance(part_ids, list)
        or not 1 <= len(part_ids) <= 3
        or any(not isinstance(key, str) or not key for key in part_ids)
    ):
        raise ValueError("Use one to three exact public observation part_ids")
    if result_path is not None and (
        not isinstance(result_path, list)
        or not 1 <= len(result_path) <= 12
        or any(
            not (
                (type(segment) is str and len(segment) <= 200)
                or (type(segment) is int and segment >= 0)
            )
            for segment in result_path
        )
    ):
        raise ValueError(
            "result_path needs 1-12 object keys or nonnegative array indices"
        )
    observations = public_observations(
        [*parent_messages, *child_messages], include_results=True
    )
    requested = list(dict.fromkeys(part_ids))
    selected = []
    unavailable_paths = []
    for key in requested:
        if key not in observations:
            continue
        observation = observations[key]
        if result_path is None:
            selected.append(observation)
            continue
        value = observation["result"]
        for depth, segment in enumerate(result_path):
            if (
                isinstance(value, dict)
                and isinstance(segment, str)
                and segment in value
            ):
                value = value[segment]
            elif (
                isinstance(value, list)
                and isinstance(segment, int)
                and segment < len(value)
            ):
                value = value[segment]
            else:
                missing: dict[str, Any] = {
                    "part_id": key,
                    "result_path": result_path,
                    "resolved_path": result_path[:depth],
                }
                if isinstance(value, dict):
                    missing["available_keys"] = [
                        field for field in value if len(field) <= 200
                    ][:25]
                    missing["key_count"] = len(value)
                elif isinstance(value, list):
                    missing["item_count"] = len(value)
                unavailable_paths.append(missing)
                break
        else:
            selected.append(
                {
                    **observation,
                    "result": value,
                    "result_path": result_path,
                    "partial_result": True,
                }
            )
    return {
        "observations": selected,
        "unavailable_part_ids": [key for key in requested if key not in observations],
        **(
            {
                "unavailable_result_paths": unavailable_paths,
                "note": "Exact selected sections, not complete sources; omitted fields are not absent or disproven. Missing paths include the resolved prefix and up to 25 available object keys or the array length, not evidence values. Correct that path or omit result_path to retrieve the full saved result. Preserve metric scope, period, units and source context when comparing claims.",
            }
            if result_path is not None
            else {}
        ),
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
            offset=request.get("offset", 0),
            limit=request.get("limit", 25),
        )
    elif view == "cases":
        result = research_notebook(**request)
    elif view == "evidence":
        result = research_observations(
            request["parent_messages"],
            request["child_messages"],
            part_ids=request.get("part_ids", []),
            result_path=request.get("result_path"),
        )
    else:
        raise ValueError("Unknown notebook view")
    print(json.dumps(result))
