"""Audit review receipts and explicit resolutions, not the truth of economic claims."""

import hashlib
import json
from typing import Any

from pydantic import ValidationError

from wayfinder_paths.core.theses.checkpoints import CaseResearch, ReviewCheckpoint
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


def decision_evidence(
    records: list[dict[str, Any]],
    research_records: list[dict[str, Any]],
    observations: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Resolve evidence/dependency links; never infer that a citation proves a claim."""
    snapshots = {}
    current = {}
    assessments: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for record in sorted(
        [*records, *research_records], key=lambda row: row["completed_at_ms"] or 0
    ):
        for field in ("research_cases", "candidates"):
            for case in record["checkpoint"][field]:
                key = case["entity"].casefold()
                snapshot = (case, record)
                snapshots[(record["session_id"], record["id"], key)] = snapshot
                if field == "candidates":
                    assessments.setdefault(key, []).append(snapshot)
                if field == "candidates" or key not in current:
                    current[key] = snapshot
                elif "decision" not in current[key][0]:
                    current[key] = snapshot
    claims = []
    updates = []
    errors = []
    requires_claims = any(
        record["checkpoint"]["schema_version"] >= 7 for record in records
    )
    for entity, (case, record) in current.items():
        if "decision" not in case:
            continue
        if (
            requires_claims
            and case["decision"] != "NEEDS_EVIDENCE"
            and not case.get("claims")
        ):
            errors.append(
                f"{entity}: current decision requires source-linked claims in this v7 run"
            )
        for claim in case.get("claims", []):
            missing = [
                part_id
                for part_id in claim["evidence_part_ids"]
                if part_id not in observations
                or (observations[part_id]["completed_at_ms"] or 0)
                > (record["completed_at_ms"] or 0)
            ]
            if missing:
                errors.append(
                    f"{entity}: claim needs earlier successful public reads, not "
                    f"unknown/private/failed/future evidence: {', '.join(missing)}"
                )
            claims.append(
                {
                    "entity": entity,
                    "checkpoint_id": record["id"],
                    **claim,
                    "unavailable_part_ids": missing,
                }
            )
        for ref in case.get("comparison_refs", []):
            other = ref["entity"].casefold()
            baseline = snapshots.get((ref["session_id"], ref["checkpoint_id"], other))
            if baseline is None or (baseline[1]["completed_at_ms"] or 0) > (
                record["completed_at_ms"] or 0
            ):
                errors.append(
                    f"{entity}: comparison_ref is not an earlier saved case: {other}"
                )
                continue
            latest, latest_record = current[other]
            fields = [*CaseResearch.model_fields, "case_basis"]
            changed = [
                field for field in fields if baseline[0].get(field) != latest.get(field)
            ]
            decision_baseline = baseline[0]
            if "decision" not in decision_baseline:
                # Worker refs preserve research provenance, but must not hide a
                # later change to the verdict the dependent decision relied on.
                history = assessments.get(other, [])
                decision_baseline = next(
                    (
                        assessed
                        for assessed, saved in reversed(history)
                        if (saved["completed_at_ms"] or 0)
                        <= (record["completed_at_ms"] or 0)
                    ),
                    # If not yet judged, track revisions after its first verdict.
                    history[0][0] if history else {},
                )
            if decision_baseline:
                changed.extend(
                    field
                    for field in (
                        "decision",
                        "reason",
                        "claims",
                        "implementation_checks",
                    )
                    if decision_baseline.get(field) != latest.get(field)
                )
            if changed:
                updates.append(
                    {
                        "entity": entity,
                        "compared_entity": other,
                        "compared_ref": ref,
                        "current_ref": {
                            "session_id": latest_record["session_id"],
                            "checkpoint_id": latest_record["id"],
                            "entity": other,
                        },
                        "changed_fields": changed,
                    }
                )
    return {
        "claims": claims,
        "claim_count": len(claims),
        "comparison_updates": updates,
        "errors": errors,
        "note": "Links establish provenance, not truth. Review decisive saved observations against each claim's scope and inference. Comparison updates identify changed inputs, not a changed verdict; check the affected comparison and current weights with the same reviewer.",
    }


def review_report(
    parent: list[dict[str, Any]],
    children: list[dict[str, Any]],
    records: list[dict[str, Any]],
    selected: set[str],
    *,
    revision: str | None = None,
) -> dict[str, Any]:
    from wayfinder_paths.core.theses.assessment import checkpoints

    findings: dict[tuple[str, str], dict[str, Any]] = {}
    reviews: set[str] = set()
    reviewed_revisions: dict[str, tuple[int, str]] = {}
    draft_reads: dict[tuple[str, str], list[int]] = {}
    observations = public_observations([*parent, *children])
    decisions = decision_evidence(records, checkpoints(children), observations)
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
                if all(f.scope == "assessment" for f in checkpoint.findings):
                    # Older model dumps included a null revision but no scope.
                    digests.add(
                        hashlib.sha256(
                            checkpoint.model_dump_json(
                                exclude={"findings": {"__all__": {"scope"}}}
                            ).encode()
                        ).hexdigest()
                    )
                if output.get("result", {}).get("sha256") not in digests:
                    continue
            except (ValueError, TypeError, ValidationError):
                continue
            session = info["sessionID"]
            reviews.add(session)
            completed_at = state.get("time", {}).get("end", 0) or 0
            if (
                checkpoint.reviewed_revision is not None
                and any(
                    timestamp <= completed_at
                    for timestamp in draft_reads.get(
                        (session, checkpoint.reviewed_revision), []
                    )
                )
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
    errors = list(decisions["errors"])
    warnings = []
    current_signed = revision is not None and any(
        reviewed_revision == revision
        for _, reviewed_revision in reviewed_revisions.values()
    )
    if not reviews:
        errors.append(
            "Reviewer must record research_thesis_review, including an empty findings list when clear"
        )
    if revision is not None and not current_signed:
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
            if action == "changed" and finding["scope"] == "draft":
                updated_draft = any(
                    r["checkpoint"]["draft"] or r["checkpoint"]["proposal"]
                    for r in records
                    if (finding["completed_at_ms"] or 0)
                    <= (r["completed_at_ms"] or 0)
                    <= resolved_at
                )
                valid &= updated_draft
                if not updated_draft:
                    errors.append(
                        f"Review finding {key[1]} ({finding['entity']}): update the "
                        "draft before resolving a draft-only finding as changed; "
                        "do not rewrite an unaffected assessment"
                    )
            elif action == "changed" and case is not None:
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
        # A typo must not permanently poison an append-only ledger. It still
        # cannot close any actual finding: all real findings need valid exact-key
        # resolutions and the single reviewer must have signed this revision.
        if (
            current_signed
            and len(reviews) == 1
            and findings
            and all(finding["resolved"] for finding in findings.values())
        ):
            warnings.append(
                f"Unmatched resolution {key[0]}/{key[1]} retained for audit; "
                "does not resolve any finding. All actual findings have valid "
                "resolutions and the current revision has reviewer sign-off."
            )
            continue
        errors.append(
            f"Resolution references unknown review finding {key[0]}/{key[1]}; "
            "resolve actual findings using exact IDs from status, then resume the SAME "
            "reviewer to sign the current revision. Unmatched entries remain in the audit."
        )
    changes = None
    if reviewed_revisions:
        session, (signed_at, baseline) = max(
            reviewed_revisions.items(), key=lambda item: item[1][0]
        )
        # Use the actual draft read, not the later receipt: a concurrent update
        # between reading and signing must still be visible. This is a retrieval
        # index of accepted parent writes, not a semantic diff or review verdict.
        read_at = max(
            stamp for stamp in draft_reads[(session, baseline)] if stamp <= signed_at
        )
        updated = [r for r in records if (r["completed_at_ms"] or 0) > read_at]
        changes = {
            "baseline_revision": baseline,
            "baseline_read_at_ms": read_at,
            "parent_checkpoint_ids": [r["id"] for r in updated],
            "parent_stages": sorted({r["checkpoint"]["stage"] for r in updated}),
            "parent_entity_keys": sorted(
                {
                    case["entity"].casefold()
                    for r in updated
                    for field in ("discoveries", "research_cases", "candidates")
                    for case in r["checkpoint"][field]
                }
                | {
                    entity.casefold()
                    for r in updated
                    for disposition in r["checkpoint"]["discovery_dispositions"]
                    for entity in disposition["entities"]
                }
            ),
            "note": "Accepted parent updates only, not a semantic diff. Check dependent comparisons even when their cases were not updated; use the current draft and saved observations, not this index, to judge the revision.",
        }
    return {
        "findings": list(findings.values()),
        "revision": revision,
        "reviewed_revisions": {
            session: value[1] for session, value in reviewed_revisions.items()
        },
        "changes_since_review": changes,
        "decision_evidence": decisions,
        "errors": errors,
        "warnings": warnings,
        "public_observations": list(observations.values()),
    }
