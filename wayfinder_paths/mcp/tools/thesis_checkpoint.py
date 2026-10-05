"""Stateless research progress: persisted only by the existing OpenCode transcript."""

import hashlib

from wayfinder_paths.core.theses.checkpoints import (
    DiscoveryCheckpoint,
    ResearchCheckpoint,
    ReviewCheckpoint,
)
from wayfinder_paths.mcp.utils import catch_errors, err, ok


@catch_errors
async def research_thesis_checkpoint(checkpoint: ResearchCheckpoint) -> dict:
    """Record typed research progress, NOT verified evidence or execution approval.

    Parent only; use schema_version=8. Record spec and inferred construction ONCE
    in interpretation; subsequent checkpoints inherit construction when omitted. Record the
    interpretation, then small draft updates (metadata/components or ONE allocation)
    and incremental judgments. Use thesis_notebook(view="status") before finishing
    with its proposal_ref; no complete portfolio JSON rewrite is required.
    Before giving named leads to workers, register them as parent discoveries.
    Every such assigned lead needs a decision or explicit out_of_scope/needs_evidence
    disposition, even if no worker ranks it. Assess every ranked research case and
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
    Decisions other than NEEDS_EVIDENCE need 1-4 claims: {statement,basis:
    observation/inference,scope,evidence_part_ids:[1-3 exact saved public read IDs]}.
    Scope states the metric definition, period/denominator/recipient when relevant;
    no numeric revenue requirement for narrative, catalyst or hedge cases. A source
    link is not proof. Include contrary evidence when it determines selection/weight.
    comparison_refs=[{session_id,checkpoint_id,entity}] links the actual saved cases
    used in comparative decisions; copy current case references from the notebook,
    never guess IDs. Changed inputs appear in review.decision_evidence.comparison_updates.
    Original research is resolved in code. Only provide updated_research when facts
    change; valid corrections persist through later decisions for the same entity
    and research_ref. A different research_ref starts from that saved source.
    For descriptive corrections, updated_research may contain only the changed
    mechanism, observed_identifiers, value_capture, support, counterevidence,
    closest_alternative or gaps. Omitted/null fields stay unchanged; gaps=[] clears
    stale gaps. Correct ALL affected fields together, not just reason/claims.
    These partial corrections preserve implementation_checks/comparison_refs;
    supply their nonempty replacements separately when they change. A complete
    ResearchCase still replaces all research and clears omitted comparison lists.
    Neither form verifies facts or waives current reviewer sign-off.
    Record review_resolutions with exact public evidence_part_ids from case
    source_reads or claim_sources (exact tool/request metadata for visible claims,
    not verification of support); only use the status observation index when
    that source is not already linked. Read the original result before citing it.
    Never guess a tool-part ID. accepted is only for nonblocking uncertainty.
    handoff_gaps acknowledges an incomplete worker after one targeted continuation,
    never fabricated research.
    Call shapes: checkpoint={schema_version:8,stage:"discovery",discoveries:[...]};
    checkpoint={schema_version:8,stage:"judged",decisions:[...]} (at most six cases);
    checkpoint={schema_version:8,stage:"draft",draft:{metadata:...,components:[...]}};
    checkpoint={schema_version:8,stage:"draft",draft:{variant:...}} (one budget).
    When the same allocation AND rationale fit several budgets, add
    draft.variant_budgets=[100,1000,10000,100000] (or an explicit subset including
    variant.budget_usd). Only those budgets are replaced; all still pass separate
    publication checks. Use separate writes where sizing or rationale differs.
    Draft updates infer omitted schema_version=7 and stage="draft"; explicit headers
    must still match the payload. Other stages and legacy versions are unchanged.
    Proposal metadata has schema_version=1 inside draft.metadata, never draft itself.
    Do not put a draft under judged or a worker handoff under a parent stage.
    After any review correction, resume the SAME reviewer to sign the new
    review.revision from notebook status. Parent resolution receipts aren't approval.
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
    Use schema_version=6; omit spec when the parent's interpretation is unchanged.
    Preserve assigned parent entity keys. Every assigned lead must appear in saved
    cases or unresolved inventory, even if it is not among your strongest cases.
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

    Read thesis_notebook(view="draft") and include reviewed_revision copied exactly
    from its review.revision. On a delta continuation, check changed decisions, actual weights,
    dependent alternatives and each proposed resolution. Do not clear a material
    objection merely because its disclosure/label changed. Reissue unsolved findings
    under their original IDs; an empty list never erases earlier unresolved findings.
    Use exact assessed entity keys. Identity, missing economic links and unsupported
    implementation exclusions can block selection; disclosed noncritical uncertainty
    need not. Record an empty findings list when clear. Parent resolves findings;
    a smaller weight alone does not supply missing proof. No trades or authorization.
    For v8 runs, copy case_ref/input_digest from review.case_checks.required and
    record case_checks=[{case_ref,input_digest,evidence_reads:[{part_id,result_path}],
    conclusion: supports/needs_change/unresolved,reason}]. Retrieve the current case
    and decisive saved source sections before checking them. Cover selected cases,
    including at least one retrieved source for each decisive claim,
    their referenced challengers and affected comparison_updates; follow next_offset.
    supports means the current assessment is supported (including a rejection),
    not that every case is a holding. Missing proof remains unresolved. Code checks
    reads and currency, not economic truth. Unchanged checked=true entries can be
    reused; changed digests require a new check by this SAME reviewer.
    """
    if checkpoint.reviewed_revision is None:
        return err(
            "invalid_argument",
            'checkpoint.reviewed_revision is required. Read thesis_notebook(view="draft") '
            "and put review.revision inside checkpoint alongside findings, not beside checkpoint.",
        )
    return ok(
        {
            "sha256": hashlib.sha256(checkpoint.receipt_json().encode()).hexdigest(),
            "reviewed_revision": checkpoint.reviewed_revision,
            "finding_count": len(checkpoint.findings),
            "case_check_count": len(checkpoint.case_checks),
            "finding_keys": [
                {
                    "finding_id": finding.id,
                    "entity": finding.entity,
                    "blocking": finding.blocking,
                }
                for finding in checkpoint.findings
            ],
            "execution_authorized": False,
        }
    )
