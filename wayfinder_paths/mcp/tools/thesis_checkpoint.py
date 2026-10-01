"""Stateless research progress: persisted only by the existing OpenCode transcript."""

import hashlib

from wayfinder_paths.core.theses.checkpoints import ResearchCheckpoint
from wayfinder_paths.mcp.utils import catch_errors, ok


@catch_errors
async def research_thesis_checkpoint(checkpoint: ResearchCheckpoint) -> dict:
    """Record typed research progress, NOT verified evidence or execution approval.

    Use schema_version=2. Workers submit discovery inventories BEFORE ranking and
    append newly found relevant entities/implementations before returning. Parent
    submits interpretation, merged discovery, provisional draft, and full judged
    ledger. Every discovered entity needs a disposition, including NEEDS_EVIDENCE.
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
    """
    return ok(
        {
            "stage": checkpoint.stage,
            "sha256": hashlib.sha256(checkpoint.model_dump_json().encode()).hexdigest(),
            "candidate_count": len(checkpoint.candidates),
            "discovery_count": len(checkpoint.discoveries),
            "execution_authorized": False,
            "evidence_verified": False,
        }
    )
