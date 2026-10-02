"""Project and validate the existing transcript; no additional mutable store."""

import hashlib
import json
from typing import Any

from pydantic import ValidationError

from wayfinder_paths.core.theses.assessment import assessment_report, projected_records
from wayfinder_paths.core.theses.models import BUDGETS, Construction
from wayfinder_paths.core.theses.publication import validate_proposal
from wayfinder_paths.core.theses.research import (
    RESEARCH_EVIDENCE_TOOLS,
    research_evidence,
)
from wayfinder_paths.core.theses.response import parse_proposal_response
from wayfinder_paths.core.theses.review import review_report


def draft_context(
    parent_messages: list[dict[str, Any]], child_messages: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    records, research_records, projection_errors = projected_records(
        parent_messages, child_messages
    )
    payload: dict[str, Any] = {}
    components: dict[str, dict[str, Any]] = {}
    variants: dict[int, dict[str, Any]] = {}
    construction = None
    current = None
    errors = list(projection_errors)
    modern = [r for r in records if r["checkpoint"]["schema_version"] >= 4]
    if modern and modern[0]["checkpoint"]["stage"] != "interpretation":
        errors.append(
            "Record the inferred construction in an interpretation checkpoint first"
        )
    for record in records:
        cp = record["checkpoint"]
        if cp.get("construction"):
            current = Construction.model_validate(cp["construction"])
            if construction is None:
                construction = current
            elif not construction.benchmark_instrument_id and all(
                getattr(current, key) == getattr(construction, key)
                for key in ("mode", "benchmark", "benchmark_direction")
            ):
                construction = current
        if cp["proposal"]:
            proposal = cp["proposal"]
            payload.update(
                {
                    k: v
                    for k, v in proposal.items()
                    if k not in {"components", "variants"}
                }
            )
            components = {c["id"]: c for c in proposal["components"]}
            variants = {v["budget_usd"]: v for v in proposal["variants"]}
        if update := cp.get("draft"):
            if update["metadata"]:
                payload.update(update["metadata"])
            for key in update["remove_components"]:
                components.pop(key, None)
            components.update((c["id"], c) for c in update["components"])
            if update["variant"]:
                variants[update["variant"]["budget_usd"]] = update["variant"]
    payload.update(
        components=list(components.values()),
        variants=[variants[b] for b in BUDGETS if b in variants],
    )
    parts = sorted(
        (
            p
            for m in [*parent_messages, *child_messages]
            for p in m.get("parts", [])
            if p.get("type") == "tool"
        ),
        key=lambda p: p.get("state", {}).get("time", {}).get("end", 0),
    )
    results = []
    for part in parts:
        state = part.get("state", {})
        if (
            part.get("tool", "").removeprefix("wayfinder_")
            not in RESEARCH_EVIDENCE_TOOLS
            or state.get("status") != "completed"
        ):
            continue
        try:
            output = json.loads(state.get("output", ""))
        except (ValueError, TypeError):
            continue
        if (
            isinstance(output, dict)
            and output.get("ok") is True
            and isinstance(output.get("result"), dict)
        ):
            results.append(output["result"])
    evidence = research_evidence(results)
    evidence["assessment"] = assessment_report(parent_messages, child_messages)
    if construction:
        evidence["construction"] = construction.model_dump(mode="json")
        if current != construction:
            errors.append(
                "Construction changed after interpretation/binding; restore the frozen construction"
            )
    # A completed reviewer must actually read the selected cases, not just an index.
    reviewed: set[str] = set()
    review_sessions = {
        m["info"]["sessionID"]
        for m in child_messages
        if m.get("info", {}).get("agent") == "thesis-reviewer"
        and m["info"].get("finish") == "stop"
        and not m["info"].get("error")
    }
    for message in child_messages:
        if message.get("info", {}).get("sessionID") not in review_sessions:
            continue
        for part in message.get("parts", []):
            state = part.get("state", {})
            if (
                part.get("tool") != "thesis_notebook"
                or state.get("status") != "completed"
            ):
                continue
            try:
                result = json.loads(state.get("output", ""))
            except (ValueError, TypeError):
                continue
            if isinstance(result, dict):
                reviewed.update(
                    row["entity"].casefold()
                    for row in result.get("cases", [])
                    if row.get("records")
                )
    cases = {
        c["entity"].casefold(): c
        for r in records
        if r["checkpoint"]["stage"] == "judged"
        for c in r["checkpoint"]["candidates"]
    }
    links = {
        key.casefold(): d["candidate_entity"].casefold()
        for r in records
        for d in r["checkpoint"]["discovery_dispositions"]
        if d["status"] == "assessed"
        for key in d["entities"]
    }
    reviewed |= {links[key] for key in reviewed if key in links}
    identities = {
        key: token["token_id"] for key, token in evidence["onchain_tokens"].items()
    }
    selected = {
        identities.get(p["instrument_id"], p["instrument_id"])
        for v in variants.values()
        for p in v["positions"]
    }
    unread = sorted(
        key
        for key, case in cases.items()
        if {identities.get(i, i) for i in case["instruments"]} & selected
        and case["decision"] == "KEEP"
        and key not in reviewed
    )
    if modern and selected:
        if not review_sessions:
            errors.append("Complete the native thesis-reviewer before finalizing")
        elif unread:
            errors.append("Reviewer did not read selected cases: " + ", ".join(unread))
    evidence["review"] = {
        "completed": bool(review_sessions),
        "read_entities": sorted(reviewed),
        "unread_selected": unread,
    }
    if any(r["checkpoint"]["schema_version"] >= 5 for r in records):
        evidence["require_implementation_comparisons"] = True
        revision = None
        if any(r["checkpoint"]["schema_version"] >= 6 for r in records):
            # Include resolutions as well as decisions: citing a public read is
            # not proof it resolves the finding. Reviewer receipts are excluded,
            # so a final sign-off doesn't invalidate its own revision.
            revision = hashlib.sha256(
                json.dumps(
                    sorted(
                        (r["session_id"] or "", r["id"] or "")
                        for r in [*records, *research_records]
                    ),
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
        review = review_report(
            parent_messages, child_messages, records, selected, revision=revision
        )
        evidence["review"].update(review)
        errors.extend(review["errors"])
    evidence["draft_errors"] = list(dict.fromkeys(errors))
    sessions = {r["session_id"] for r in records}
    reference = None
    if modern and len(sessions) == 1 and None not in sessions:
        reference = hashlib.sha256(
            json.dumps(
                [
                    sorted(sessions),
                    [r["id"] for r in records],
                    evidence.get("construction"),
                    payload,
                ],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    elif modern:
        evidence["draft_errors"].append(
            "Cannot establish a single parent draft session"
        )
    return payload, evidence, reference


def draft_status(
    parent_messages: list[dict[str, Any]],
    child_messages: list[dict[str, Any]],
    *,
    include_proposal: bool = False,
    offset: int = 0,
    limit: int = 25,
) -> dict[str, Any]:
    if offset < 0 or not 1 <= limit <= 100:
        raise ValueError("Use offset>=0 and limit 1..100 for notebook pages")
    payload, evidence, reference = draft_context(parent_messages, child_messages)
    errors = []
    try:
        validate_proposal(payload, evidence)
    except ValueError as exc:
        errors = str(exc).splitlines()
    ready = not errors and reference is not None
    review = dict(evidence["review"])
    # Old resolved findings can outgrow the portfolio. Page presentation only;
    # validation above still considers every finding and its exact resolution.
    findings = sorted(
        review.get("findings", []),
        key=lambda row: (
            row["resolved"],
            not row["blocking"],
            row["review_session_id"],
            row["id"],
        ),
    )
    review["findings"] = findings[offset : offset + limit]
    review["findings_page"] = {
        "total": len(findings),
        "unresolved": sum(not row["resolved"] for row in findings),
        "unresolved_blocking": sum(
            row["blocking"] and not row["resolved"] for row in findings
        ),
        "next_offset": offset + limit if offset + limit < len(findings) else None,
        "order": "unresolved_first_then_blocking_then_session_and_id",
        "note": "Counts and publication errors cover all findings, not just this page. Follow next_offset for full finding and resolution text; resolved is bookkeeping, not verified truth.",
    }
    if "decision_evidence" in review:
        # Full claims remain in case views, not duplicated beside four portfolios.
        decision_evidence = review["decision_evidence"]
        review["decision_evidence"] = {
            key: value for key, value in decision_evidence.items() if key != "claims"
        }
        if include_proposal:
            claim_refs: dict[str, dict[str, Any]] = {}
            for claim in decision_evidence["claims"]:
                row = claim_refs.setdefault(
                    claim["entity"],
                    {
                        "entity": claim["entity"],
                        "checkpoint_id": claim["checkpoint_id"],
                        "evidence_part_ids": [],
                    },
                )
                row["evidence_part_ids"] = sorted(
                    set(row["evidence_part_ids"]) | set(claim["evidence_part_ids"])
                )
            index = [claim_refs[key] for key in sorted(claim_refs)]
            review["decision_evidence"].update(
                claim_index=index[offset : offset + limit],
                claim_index_page={
                    "total": len(index),
                    "next_offset": offset + limit
                    if offset + limit < len(index)
                    else None,
                    "order": "entity_asc",
                    "read_claims": "Use view=cases with exact entity keys; claims survive field projection. This index is not the evidence or a completed claim review.",
                },
            )
    # Fresh correction reads should not require paging through the whole run.
    # Sort only the presentation; publication still audits every observation.
    observations = sorted(
        review.get("public_observations", []),
        key=lambda row: row.get("completed_at_ms") or 0,
        reverse=True,
    )
    review["public_observations"] = observations[offset : offset + limit]
    review["public_observations_page"] = {
        "total": len(observations),
        "next_offset": offset + limit if offset + limit < len(observations) else None,
        "order": "newest_first",
    }
    return {
        "ready": ready,
        "proposal_ref": reference if ready else None,
        "errors": errors,
        "construction": evidence.get("construction"),
        "budgets_recorded": [v["budget_usd"] for v in payload["variants"]],
        "assessment": evidence["assessment"],
        "review": review,
        **({"proposal": payload} if include_proposal else {}),
        "execution_authorized": False,
    }


def publication_result(
    text: str,
    parent_messages: list[dict[str, Any]],
    child_messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Decode the explicit final response; saved drafts are diagnostics, not fallback publication."""
    draft, evidence, reference = draft_context(parent_messages, child_messages)
    proposal = None
    parse_error = None
    try:
        proposal = parse_proposal_response(text)
        if "proposal_ref" in proposal:
            if (
                set(proposal) != {"proposal_ref"}
                or reference is None
                or proposal["proposal_ref"] != reference
            ):
                raise ValueError(
                    "Unknown or stale proposal_ref; read thesis_notebook(view='status') for the latest validated draft"
                )
            proposal = draft
        elif reference is not None and proposal != draft:
            raise ValueError(
                "Final portfolio differs from the saved draft; record changes and read "
                "thesis_notebook(view='status') before returning its proposal_ref"
            )
    except (ValueError, ValidationError) as exc:
        parse_error = str(exc)
        proposal = None
    errors = (
        [f"Final answer must be a JSON portfolio object or proposal_ref: {parse_error}"]
        if parse_error
        else []
    )
    try:
        validate_proposal(proposal if proposal is not None else draft, evidence)
    except ValueError as exc:
        errors.append(str(exc))
    return {
        "proposal": proposal,
        "market_evidence": evidence,
        "parse_error": parse_error,
        "feedback": "\n".join(dict.fromkeys(errors)),
    }
