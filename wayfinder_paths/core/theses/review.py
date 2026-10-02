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


def public_observations(
    messages: list[dict[str, Any]], *, include_results: bool = False
) -> dict[str, dict[str, Any]]:
    """Usable public reads, shared by resolution validation and notebook retrieval."""
    observations = {}
    for message in messages:
        for part in message.get("parts", []):
            state = part.get("state", {})
            tool = part.get("tool", "")
            if (
                state.get("status") != "completed"
                or tool.removeprefix("wayfinder_") not in REVIEW_EVIDENCE_TOOLS
                or not part.get("id")
            ):
                continue
            try:
                output = json.loads(state.get("output", ""))
            except (ValueError, TypeError):
                continue
            if not isinstance(output, dict) or output.get("ok") is not True:
                continue
            data = output.get("result")
            if not data:
                continue
            if tool.removeprefix("wayfinder_") == "core_web_fetch" and not (
                isinstance(data, dict)
                and isinstance(data.get("results"), list)
                and any(
                    row.get("contentExcerpt") or row.get("text") or row.get("content")
                    for row in data.get("results", [])
                    if isinstance(row, dict)
                )
            ):
                continue
            observations[part["id"]] = {
                "part_id": part["id"],
                "tool": tool,
                "completed_at_ms": state.get("time", {}).get("end"),
                # Do not expose unrelated tool arguments or configuration.
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
                **({"result": data} if include_results else {}),
            }
    return observations


def review_report(
    parent: list[dict[str, Any]],
    children: list[dict[str, Any]],
    records: list[dict[str, Any]],
    selected: set[str],
    *,
    revision: str | None = None,
) -> dict[str, Any]:
    findings: dict[tuple[str, str], dict[str, Any]] = {}
    reviews: set[str] = set()
    reviewed_revisions: dict[str, tuple[int, str | None]] = {}
    draft_reads: dict[tuple[str, str], list[int]] = {}
    observations = public_observations([*parent, *children])
    for message in children:
        info = message.get("info", {})
        if info.get("agent") != "thesis-reviewer":
            continue
        for part in message.get("parts", []):
            state = part.get("state", {})
            if (
                part.get("tool") != "thesis_notebook"
                or state.get("status") != "completed"
                or state.get("input", {}).get("view") != "draft"
            ):
                continue
            try:
                output = json.loads(state.get("output", ""))
            except (TypeError, ValueError):
                continue
            if not isinstance(output, dict) or not isinstance(
                output.get("proposal"), dict
            ):
                continue
            read_revision = output.get("review", {}).get("revision")
            if isinstance(read_revision, str):
                draft_reads.setdefault(
                    (info.get("sessionID"), read_revision), []
                ).append(state.get("time", {}).get("end", 0) or 0)
    for message in [*parent, *children]:
        info = message.get("info", {})
        for part in message.get("parts", []):
            state = part.get("state", {})
            if (
                state.get("status") != "completed"
                or part.get("tool") != REVIEW_TOOL
                or info.get("agent") != "thesis-reviewer"
                or not info.get("sessionID")
            ):
                continue
            try:
                output = json.loads(state.get("output", ""))
                if not isinstance(output, dict) or output.get("ok") is not True:
                    continue
                checkpoint = ReviewCheckpoint.model_validate(
                    state.get("input", {}).get("checkpoint")
                )
                digests = {
                    hashlib.sha256(serialized.encode()).hexdigest()
                    for serialized in (
                        checkpoint.model_dump_json(),
                        checkpoint.receipt_json(),
                    )
                }
                if output.get("result", {}).get("sha256") not in digests:
                    continue
            except (ValueError, TypeError, ValidationError):
                continue
            session = info["sessionID"]
            reviews.add(session)
            completed_at = state.get("time", {}).get("end", 0) or 0
            read_before_signoff = any(
                timestamp <= completed_at
                for timestamp in draft_reads.get(
                    (session, checkpoint.reviewed_revision), []
                )
            )
            if (
                read_before_signoff
                and completed_at >= reviewed_revisions.get(session, (0, None))[0]
            ):
                reviewed_revisions[session] = (
                    completed_at,
                    checkpoint.reviewed_revision,
                )
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
    resolutions: dict[tuple[str, str], tuple[dict[str, Any], int]] = {}
    first_resolution_at: dict[tuple[str, str], int] = {}
    for record in records:
        for resolution in record["checkpoint"]["review_resolutions"]:
            key = (resolution["review_session_id"], resolution["finding_id"])
            timestamp = record["completed_at_ms"] or 0
            resolutions[key] = (resolution, timestamp)
            first_resolution_at.setdefault(key, timestamp)
    errors = []
    if not reviews:
        errors.append(
            "Reviewer must record research_thesis_review, including an empty findings list when clear"
        )
    if revision is not None and not any(
        reviewed_revision == revision
        for _, reviewed_revision in reviewed_revisions.values()
    ):
        errors.append(
            "Current decisions/draft/resolutions need native reviewer sign-off: "
            "resume the SAME reviewer for a focused delta check, read view=draft, and record "
            f"reviewed_revision={revision}. Do not self-certify a changed rationale."
        )
    if revision is not None and len(reviews) > 1:
        errors.append(
            "Use one native reviewer; resume its existing task for delta checks"
        )
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
    for key in resolutions.keys() - findings.keys():
        matches = [k for k in findings if k[1] == key[1]]
        # An append-only transcript preserves a mistyped session ID. A later,
        # valid resolution of the unique real finding supersedes that typo;
        # never infer a resolution from the malformed reference itself.
        if key[0] not in reviews and len(matches) == 1:
            corrected = matches[0]
            if (
                findings[corrected]["resolved"]
                and resolutions[corrected][1] > first_resolution_at[key]
            ):
                continue
        errors.append(
            f"Resolution references unknown review finding {key[0]}/{key[1]}; "
            "resubmit using the exact review_session_id and finding_id from status"
        )
    return {
        "findings": list(findings.values()),
        "revision": revision,
        "reviewed_revisions": {
            session: value[1] for session, value in reviewed_revisions.items()
        },
        "errors": errors,
        "public_observations": list(observations.values()),
    }
