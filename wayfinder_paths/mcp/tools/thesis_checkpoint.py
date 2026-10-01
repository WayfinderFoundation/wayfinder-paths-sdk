"""Stateless research progress: persisted only by the existing OpenCode transcript."""

import hashlib

from wayfinder_paths.core.theses.checkpoints import (
    DiscoveryCheckpoint,
    ResearchCheckpoint,
    ReviewCheckpoint,
)
from wayfinder_paths.mcp.utils import catch_errors, ok


@catch_errors
async def research_thesis_checkpoint(checkpoint: ResearchCheckpoint) -> dict:
    """Record typed research progress, NOT verified evidence or execution approval.

    Parent only; use schema_version=5 and an inferred construction. Record the
    interpretation, then small draft updates (metadata/components or ONE variant)
    and incremental judgments. Use thesis_notebook(view="status") before finishing
    with its proposal_ref; no complete portfolio JSON rewrite is required.
    Assess every ranked research case and
    final holding; unranked leads remain visible without invented dispositions.
    Use discovery_dispositions to link differing worker entity keys to assessed
    candidate_entity keys, or briefly mark unranked leads out_of_scope/needs_evidence.
    Do not duplicate full candidate essays for the entire discovery inbox.
    Later judged checkpoints upsert cases and dispositions by entity key; send
    changed entries without repeating unchanged ones. Omission never deletes them.
    Implementation-driven exclusions must compare known alternatives, including
    spot versus perp where applicable, instead of rejecting the whole exposure.
    Inputs remain agent assertions: only independent public tool reads verify them.
    This tool does not write files, publish a portfolio, fetch data or access wallets.
    The transcript stores the input; a small receipt avoids duplicating the ledger.
    Receipt counts cover this call only: research_case_count counts ranked research,
    candidate_count counts parent judgments, and handoff_recorded confirms a manifest.
    Prefer decisions=[{research_ref:{session_id,checkpoint_id,entity},entity,
    decision,decision_basis,reason,implementation_checks}] using notebook references.
    Original research is resolved in code. Only provide updated_research when facts
    change. Record review_resolutions with exact public evidence_part_ids from
    notebook status; match request_summary and paginate public_observations_page.
    Never guess a tool-part ID. accepted is only for nonblocking uncertainty.
    handoff_gaps acknowledges an incomplete worker after one targeted continuation,
    never fabricated research.
    """
    return ok(
        {
            "stage": checkpoint.stage,
            "schema_version": checkpoint.schema_version,
            "sha256": hashlib.sha256(checkpoint.receipt_json().encode()).hexdigest(),
            "candidate_count": len(checkpoint.candidates) + len(checkpoint.decisions),
            "discovery_count": len(checkpoint.discoveries),
            "research_case_count": len(checkpoint.research_cases),
            "handoff_recorded": checkpoint.handoff is not None,
            "execution_authorized": False,
            "evidence_verified": False,
        }
    )


@catch_errors
async def research_thesis_discovery(checkpoint: DiscoveryCheckpoint) -> dict:
    """Record discoveries and up to ten evidence-backed ranked research cases.

    Append new leads/implementations before ranking. Record comparisons in
    research_cases: case_basis (economic/narrative/mixed/hedge/event), mechanism,
    value_capture, dated support, counterevidence, closest_alternative and gaps.
    No minimum count. Category membership alone does not qualify a ranked case.
    Narrative cases need evidence of attention/capital flows, timing and
    invalidation, not invented revenue. Every ranked case needs a parent decision.
    Return a compact handoff referencing recorded entity keys, not the full inbox.
    This stores assertions only in the transcript, never parent judgments,
    files, trades or independently verified evidence.
    Use schema_version=5; omit spec when the parent's interpretation is unchanged.
    Save cases progressively, then finish with handoff:
    case_entities lists EVERY saved ranked key; unresolved_entities lists remaining
    inventory keys; reason briefly explains gaps (or that research is complete). An inbox
    without ranked cases is not a completed assessment. Do not repeat case essays.
    The receipt's research_case_count confirms ranked cases in this call;
    candidate_count is for parent judgments and is always zero for this worker tool.
    handoff_recorded confirms a manifest, including a handoff-only write. These are
    per-call counts, not the cumulative notebook inventory; zero new cases in a
    handoff-only receipt does not erase previously saved research.
    """
    return await research_thesis_checkpoint(
        ResearchCheckpoint.model_validate(checkpoint.model_dump())
    )


@catch_errors
async def research_thesis_review(checkpoint: ReviewCheckpoint) -> dict:
    """Reviewer only: persist findings (id, entity, blocking, issue, required_change).

    Use exact assessed entity keys. Identity, missing economic links and unsupported
    implementation exclusions can block selection; disclosed noncritical uncertainty
    need not. Record an empty findings list when clear. Parent resolves findings;
    a smaller weight alone does not supply missing proof. No trades or authorization.
    """
    return ok(
        {
            "sha256": hashlib.sha256(checkpoint.model_dump_json().encode()).hexdigest(),
            "finding_count": len(checkpoint.findings),
            "execution_authorized": False,
        }
    )
