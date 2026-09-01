"""Benchmark MCP surface: campaign lifecycle only, no market-data tools."""

from __future__ import annotations

import argparse
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from wayfinder_paths.mcp.tools.jobs import core_jobs as _production_core_jobs

BenchAction = Literal[
    "evolution_status",
    "evolution_design",
    "evolution_prepare",
    "evolution_submit_seed",
    "evolution_evaluate",
    "evolution_finalize",
]
_ALLOWED_ACTIONS = {
    "evolution_status",
    "evolution_design",
    "evolution_prepare",
    "evolution_submit_seed",
    "evolution_evaluate",
    "evolution_finalize",
}


async def core_jobs(
    action: BenchAction,
    *,
    job_id: str,
    campaign_design: dict[str, Any] | None = None,
    family: str | None = None,
    summary: str | None = None,
    mutation_kind: Literal["structural", "parameter"] | None = None,
    candidate_dir: str | None = None,
    candidate_id: str | None = None,
    hypothesis: str | None = None,
    base_revision: str | None = None,
    evidence_refs: list[str] | None = None,
    background: bool | None = None,
) -> dict[str, Any]:
    """Production evolution lifecycle with all data/live actions absent."""
    if action not in _ALLOWED_ACTIONS:
        raise ValueError(f"action {action!r} is unavailable in benchmark mode")
    return await _production_core_jobs(
        action,
        job_id=job_id,
        campaign_design=campaign_design,
        family=family,
        summary=summary,
        mutation_kind=mutation_kind,
        candidate_dir=candidate_dir,
        candidate_id=candidate_id,
        hypothesis=hypothesis,
        base_revision=base_revision,
        evidence_refs=evidence_refs,
        background=background,
    )


def build_bench_mcp(*, host: str, port: int) -> FastMCP:
    server = FastMCP("wayfinder-bench", host=host, port=port)
    server.tool()(core_jobs)
    return server


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args()
    build_bench_mcp(host=args.host, port=args.port).run(transport="streamable-http")


if __name__ == "__main__":
    main()
