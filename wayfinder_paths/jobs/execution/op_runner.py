"""Run one heavy jobs operation in an isolated child process.

The MCP server is a long-lived asyncio process sharing a small (2 GB / 2 vCPU)
box with the agent runtime. Running a backtest inside it — even offloaded to a
worker thread — has two observed failure modes:

1. GIL contention with the server's event loop slows the tick loop ~28x
   (measured live: 15 bars/s in-server vs 429 bars/s standalone).
2. The memory spike of a full run gets the WHOLE server OOM-killed, silently
   dropping every wayfinder tool for the session (observed live: server died
   at backtest completion with no traceback, opencode timed out the call at
   300s, and the next call failed with "unable to connect").

Child-process isolation fixes both: the server's loop stays free, and a killed
child surfaces as a clean tool error instead of a dead server.

Protocol: JSON ``{"op": ..., "kwargs": {...}}`` on stdin; the result as JSON on
stdout. stdout carries ONLY the result JSON — backtest progress already goes to
stderr. Failures propagate as a traceback on stderr with a non-zero exit.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

_EVIDENCE_OPS = {
    "backtest_job",
    "experiments",
    "signal_scan",
    "holdout_check",
    "restamp",
    "robustness_check",
}


def _run(op: str, kwargs: dict[str, Any]) -> Any:
    if op in _EVIDENCE_OPS and kwargs.get("job_id"):
        # Every validation query is on the protected record (audit/<job_id>/)
        # — the review's evidence-access ledger. Best-effort, never blocks.
        from wayfinder_paths.jobs.governance import record_evidence_access
        from wayfinder_paths.jobs.store import JobStore

        try:
            store = JobStore()
            record_evidence_access(
                store.repo_root,
                str(kwargs["job_id"]),
                op,
                {"kwargs_keys": sorted(k for k in kwargs if k != "job_id")},
            )
        except Exception:  # noqa: BLE001
            pass
    return _run_op(op, kwargs)


def _run_op(op: str, kwargs: dict[str, Any]) -> Any:
    # Imports live inside each branch so the child only pays for what it runs.
    if op == "__echo__":
        # Health-check / test op: round-trips kwargs through the full pipe.
        return kwargs
    if op == "fetch_dataset":
        from wayfinder_paths.jobs.execution.preflight import (
            build_live_dataset,
            fetch_funding_features,
        )

        job_id = kwargs.pop("job_id")
        include_funding = bool(kwargs.pop("include_funding", False))
        result = build_live_dataset(job_id, **kwargs)
        if include_funding:
            result["funding"] = fetch_funding_features(
                job_id,
                days=int(kwargs.get("days") or 14),
                exchange=str(kwargs.get("exchange") or "binance"),
                quote=str(kwargs.get("quote") or "USDT"),
            )
        return result
    if op == "fetch_funding":
        from wayfinder_paths.jobs.execution.preflight import fetch_funding_features

        return fetch_funding_features(kwargs.pop("job_id"), **kwargs)
    if op == "pair_check":
        from wayfinder_paths.jobs.research import pair_check_job

        return pair_check_job(kwargs.pop("job_id"), **kwargs)
    if op == "signal_check":
        from wayfinder_paths.jobs.research import signal_check_job

        return signal_check_job(kwargs.pop("job_id"), **kwargs)
    if op == "signal_scan":
        from wayfinder_paths.jobs.research import signal_scan_job

        return signal_scan_job(kwargs.pop("job_id"), **kwargs)
    if op == "chart":
        from wayfinder_paths.jobs.chart import chart_job

        return chart_job(kwargs.pop("job_id"), **kwargs)
    if op == "analogs":
        from wayfinder_paths.jobs.chart import analogs_job

        return analogs_job(kwargs.pop("job_id"), **kwargs)
    if op == "derive_features":
        from wayfinder_paths.jobs.derived_features import derive_features_job

        return derive_features_job(kwargs.pop("job_id"), **kwargs)
    if op == "attribution":
        from wayfinder_paths.jobs.attribution import attribution_job

        return attribution_job(kwargs.pop("job_id"), **kwargs)
    if op == "holdout_check":
        from wayfinder_paths.jobs.research import holdout_check_job

        return holdout_check_job(kwargs.pop("job_id"), **kwargs)
    if op == "rank_check":
        from wayfinder_paths.jobs.research import rank_check_job

        return rank_check_job(kwargs.pop("job_id"), **kwargs)
    if op == "robustness_check":
        from wayfinder_paths.jobs.robustness import robustness_check_job

        return robustness_check_job(kwargs.pop("job_id"), **kwargs)
    if op == "backtest_job":
        from wayfinder_paths.jobs.execution.job import (
            backtest_execution_job,
            summarize_backtest_payload,
        )

        full = kwargs.pop("full", False)
        payload = backtest_execution_job(kwargs.pop("job_id"), **kwargs)
        return payload if full else summarize_backtest_payload(payload)
    if op == "experiments":
        from wayfinder_paths.jobs.execution.experiments import run_experiment
        from wayfinder_paths.jobs.execution.job import summarize_backtest_payload

        full = kwargs.pop("full", False)
        result = run_experiment(kwargs.pop("job_id"), kwargs.pop("grid"), **kwargs)
        backtest = result.get("backtest")
        if isinstance(backtest, dict) and not full:
            result["backtest"] = summarize_backtest_payload(backtest)
        return result
    if op == "restamp":
        # Full gate refresh after an execution_params change (leverage knob):
        # any job.yaml edit bumps the workspace revision and invalidates the
        # validation/backtest/preflight stamps — re-run all three and report
        # the resulting gate. backtest takes the heavy-compute lock itself.
        from wayfinder_paths.jobs.execution.job import backtest_execution_job
        from wayfinder_paths.jobs.execution.preflight import run_preflight
        from wayfinder_paths.jobs.execution.validation import (
            validate_execution_job,
        )
        from wayfinder_paths.jobs.gating import evaluate_live_gate

        job_id = kwargs.pop("job_id")
        validation = validate_execution_job(job_id)
        backtest = backtest_execution_job(job_id)
        preflight = run_preflight(job_id)
        return {
            "validation": (validation or {}).get("status"),
            "backtest": ((backtest.get("result") or {}).get("stats") or {}).get(
                "net_return"
            ),
            "preflight": (preflight or {}).get("status"),
            "gate": evaluate_live_gate(job_id),
        }
    if op == "promote_params":
        from wayfinder_paths.jobs.execution.experiments import promote_params

        return promote_params(kwargs.pop("job_id"), **kwargs)
    raise ValueError(f"unknown op: {op}")


def _lower_priority() -> None:
    """Deprioritize this heavy op against the long-lived server + agent it
    shares the box with, on all three contended resources: lowest CPU priority
    (yields the core the moment the interactive path needs it), first OOM
    victim (a memory spike kills this child, not the server), and 'idle' I/O
    class (disk reads yield to interactive reads). All three are unprivileged
    and best-effort — a platform missing any of them just skips that one.
    """
    try:
        os.nice(19)
    except Exception:  # noqa: BLE001
        pass
    try:
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write("1000")
    except Exception:  # noqa: BLE001
        pass
    try:
        # 'idle' I/O class via ionice(1). Only bites under an ioprio-aware I/O
        # scheduler (CFQ/BFQ); a no-op elsewhere. capture_output keeps the
        # result-only stdout contract intact.
        subprocess.run(
            ["ionice", "-c", "3", "-p", str(os.getpid())],
            check=False,
            capture_output=True,
        )
    except Exception:  # noqa: BLE001
        pass


def main() -> None:
    _lower_priority()
    request = json.load(sys.stdin)
    result = _run(request["op"], dict(request.get("kwargs") or {}))
    json.dump(result, sys.stdout, default=str)


if __name__ == "__main__":
    main()
