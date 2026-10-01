"""Stateless parent progress: persisted only by the existing OpenCode transcript."""

import hashlib

from wayfinder_paths.core.theses.checkpoints import ResearchCheckpoint
from wayfinder_paths.mcp.utils import catch_errors, ok


@catch_errors
async def research_thesis_checkpoint(checkpoint: ResearchCheckpoint) -> dict:
    """Record typed parent research progress, NOT verified evidence or execution approval.

    Submit interpretation, a provisional four-budget draft, and a judged candidate
    ledger in the same parent session. Economic entities group all implementations.
    Inputs remain agent assertions: only independent public tool reads verify them.
    This tool does not write files, publish a portfolio, fetch data or access wallets.
    The transcript stores the input; a small receipt avoids duplicating the ledger.
    """
    return ok(
        {
            "stage": checkpoint.stage,
            "sha256": hashlib.sha256(checkpoint.model_dump_json().encode()).hexdigest(),
            "candidate_count": len(checkpoint.candidates),
            "execution_authorized": False,
            "evidence_verified": False,
        }
    )
