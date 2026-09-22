"""Benchmark MCP surface: campaign lifecycle plus read-only local diagnostics,
no market-data or research tools."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from wayfinder_paths.jobs.bench.env import sandbox_relative
from wayfinder_paths.mcp.tools.jobs import core_jobs as _production_core_jobs

BenchAction = Literal[
    "status",
    "report",
    "regime_health",
    "attribution",
    "signal_check",
    "signal_scan",
    "backtest_diagnose",
    "holdout_check",
    "evolution_status",
    "evolution_design",
    "evolution_compose",
    "evolution_redesign",
    "evolution_mechanism_grid",
    "evolution_prepare",
    "evolution_submit_seed",
    "evolution_evaluate",
    "evolution_finalize",
]
_ALLOWED_ACTIONS = {
    "status",
    "report",
    "regime_health",
    "attribution",
    "signal_check",
    "signal_scan",
    "backtest_diagnose",
    "holdout_check",
    "evolution_status",
    "evolution_design",
    "evolution_compose",
    "evolution_redesign",
    "evolution_mechanism_grid",
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
    signal_proposals: list[dict[str, Any]] | None = None,
    redesign: dict[str, Any] | None = None,
    signal_ref: str | None = None,
    side: Literal["long", "short"] | None = None,
    family: str | None = None,
    summary: str | None = None,
    mutation_kind: Literal["structural", "parameter"] | None = None,
    candidate_dir: str | None = None,
    candidate_id: str | None = None,
    hypothesis: str | None = None,
    base_revision: str | None = None,
    evidence_refs: list[str] | None = None,
    background: bool | None = None,
    proposal_id: str | None = None,
    force: bool = False,
    symbols: list[str] | None = None,
    column: str | None = None,
    horizons: list[int] | None = None,
    timeframes: list[str] | None = None,
    direction: Literal["long", "short", "auto"] | None = None,
    signal: str | None = None,
    horizon: int | None = None,
    bar_interval: str | None = None,
    holdout_fraction: float = 0.15,
    campaign: str | None = None,
    condition_regime: bool = False,
    window_days: int | None = None,
) -> dict[str, Any]:
    """Production evolution lifecycle and local diagnostics with all
    data-fetching and live actions absent."""
    if action not in _ALLOWED_ACTIONS:
        raise ValueError(f"action {action!r} is unavailable in benchmark mode")
    # Design validation is lightweight. Keep it inline so a malformed design
    # returns its exact validator error to the same model turn instead of
    # depending on a future scheduler wake to discover a detached failure.
    effective_background = (
        False
        if action
        in {
            "evolution_design",
            "evolution_compose",
            "evolution_redesign",
            "evolution_mechanism_grid",
        }
        else background
    )
    result = await _production_core_jobs(
        action,
        job_id=job_id,
        campaign_design=campaign_design,
        signal_proposals=signal_proposals,
        redesign=redesign,
        signal_ref=signal_ref,
        side=side,
        family=family,
        summary=summary,
        mutation_kind=mutation_kind,
        candidate_dir=candidate_dir,
        candidate_id=candidate_id,
        hypothesis=hypothesis,
        base_revision=base_revision,
        evidence_refs=evidence_refs,
        background=effective_background,
        proposal_id=proposal_id,
        force=force,
        symbols=symbols,
        column=column,
        horizons=horizons,
        timeframes=timeframes,
        direction=direction,
        signal=signal,
        horizon=horizon,
        bar_interval=bar_interval,
        holdout_fraction=holdout_fraction,
        campaign=campaign,
        condition_regime=condition_regime,
        window_days=window_days,
    )
    return sandbox_relative(result, root=Path.cwd())


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
