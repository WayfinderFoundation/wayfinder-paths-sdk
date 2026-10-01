"""Audit review receipts and explicit resolutions, not the truth of economic claims."""

import hashlib
import json
from typing import Any

from pydantic import ValidationError

from wayfinder_paths.core.theses.checkpoints import ReviewCheckpoint
from wayfinder_paths.core.theses.research import RESEARCH_EVIDENCE_TOOLS

REVIEW_TOOL = "wayfinder_research_thesis_review"
REVIEW_EVIDENCE_TOOLS = RESEARCH_EVIDENCE_TOOLS | {
    "research_defillama_free",
    "hyperliquid_get_funding_history",
    "research_search_price",
    "research_search_perp",
}


def review_report(
    parent: list[dict[str, Any]],
    children: list[dict[str, Any]],
    records: list[dict[str, Any]],
    selected: set[str],
) -> dict[str, Any]:
    findings: dict[tuple[str, str], dict[str, Any]] = {}
    reviews: set[str] = set()
    observations: dict[str, dict[str, Any]] = {}
    for message in [*parent, *children]:
        info = message.get("info", {})
        for part in message.get("parts", []):
            state = part.get("state", {})
            if state.get("status") != "completed":
                continue
            try:
                output = json.loads(state.get("output", ""))
                if not isinstance(output, dict) or output.get("ok") is not True:
                    continue
                tool = part.get("tool", "")
                data = output.get("result")
                useful = bool(data)
                if tool == "wayfinder_core_web_fetch":
                    useful = isinstance(data, dict) and any(
                        row.get("contentExcerpt")
                        or row.get("text")
                        or row.get("content")
                        for row in data.get("results", [])
                        if isinstance(row, dict)
                    )
                if (
                    tool.removeprefix("wayfinder_") in REVIEW_EVIDENCE_TOOLS
                    and part.get("id")
                    and useful
                ):
                    observations[part["id"]] = {
                        "part_id": part["id"],
                        "tool": tool,
                        "completed_at_ms": state.get("time", {}).get("end"),
                        # Native tool-part IDs are not visible in the model's
                        # ordinary tool messages. Identify reads without raw bodies.
                        "request_summary": json.dumps(
                            {
                                key: value
                                for key, value in state.get("input", {}).items()
                                if key
                                in {
                                    "query",
                                    "urls",
                                    "dataset",
                                    "protocolSlug",
                                    "protocolSlugs",
                                    "dataType",
                                    "asset_names",
                                    "asset_name",
                                    "token_id",
                                    "market_slug",
                                    "action",
                                }
                            },
                            ensure_ascii=False,
                        )[:600],
                    }
                if (
                    tool != REVIEW_TOOL
                    or info.get("agent") != "thesis-reviewer"
                    or not info.get("sessionID")
                ):
                    continue
                checkpoint = ReviewCheckpoint.model_validate(
                    state.get("input", {}).get("checkpoint")
                )
                digest = hashlib.sha256(
                    checkpoint.model_dump_json().encode()
                ).hexdigest()
                if output.get("result", {}).get("sha256") != digest:
                    continue
            except (ValueError, TypeError, ValidationError):
                continue
            session = info["sessionID"]
            reviews.add(session)
            for item in checkpoint.findings:
                findings[(session, item.id)] = {
                    **item.model_dump(),
                    "review_session_id": session,
                    "completed_at_ms": state.get("time", {}).get("end", 0),
                }
    cases = {
        c["entity"].casefold(): c
        for r in records
        for c in r["checkpoint"]["candidates"]
    }
    resolutions = {
        (v["review_session_id"], v["finding_id"]): (v, r["completed_at_ms"] or 0)
        for r in records
        for v in r["checkpoint"]["review_resolutions"]
    }
    errors = []
    if not reviews:
        errors.append(
            "Reviewer must record research_thesis_review, including an empty findings list when clear"
        )
    for key in resolutions.keys() - findings.keys():
        errors.append(f"Resolution references unknown review finding {key[1]}")
    for key, finding in findings.items():
        resolution, resolved_at = resolutions.get(key, ({}, 0))
        action = resolution.get("action")
        refs = resolution.get("evidence_part_ids", [])
        case = cases.get(finding["entity"].casefold())
        valid = resolved_at >= (finding["completed_at_ms"] or 0)
        if action == "accepted":
            valid &= not finding["blocking"]
        elif action == "removed":
            valid &= bool(
                case
                and case["decision"] != "KEEP"
                and not (set(case["instruments"]) & selected)
            )
        elif action in {"evidence", "changed"}:
            valid &= bool(refs) and all(
                ref in observations
                and (observations[ref]["completed_at_ms"] or 0) <= resolved_at
                for ref in refs
            )
            # A label/weight-only edit without cited public observations cannot close a finding.
            if action == "changed" and case is not None:
                # The original worker record stays immutable, but the parent's
                # decision must reflect its correction, not just the final prose.
                updated_case = any(
                    c["entity"].casefold() == finding["entity"].casefold()
                    for r in records
                    if (finding["completed_at_ms"] or 0)
                    <= (r["completed_at_ms"] or 0)
                    <= resolved_at
                    for c in r["checkpoint"]["candidates"]
                )
                valid &= updated_case
                if not updated_case:
                    errors.append(
                        f"Review finding {key[1]} ({finding['entity']}): upsert the "
                        "corrected decision before resolving changed; use "
                        "updated_research when the original case facts changed"
                    )
        else:
            valid = False
        finding["resolution"] = resolution or None
        finding["resolved"] = bool(valid)
        if not valid:
            errors.append(
                f"Review finding {key[1]} ({finding['entity']}): resolve with public evidence, a supported change or removal; only nonblocking uncertainty may be accepted"
            )
    return {
        "findings": list(findings.values()),
        "errors": errors,
        "public_observations": list(observations.values()),
    }
