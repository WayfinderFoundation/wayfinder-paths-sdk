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
import sys
from typing import Any


def _run(op: str, kwargs: dict[str, Any]) -> Any:
    # Imports live inside each branch so the child only pays for what it runs.
    if op == "__echo__":
        # Health-check / test op: round-trips kwargs through the full pipe.
        return kwargs
    if op == "fetch_dataset":
        from wayfinder_paths.jobs.execution.preflight import build_live_dataset

        return build_live_dataset(kwargs.pop("job_id"), **kwargs)
    if op == "fetch_funding":
        from wayfinder_paths.jobs.execution.preflight import fetch_funding_features

        return fetch_funding_features(kwargs.pop("job_id"), **kwargs)
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
    if op == "promote_params":
        from wayfinder_paths.jobs.execution.experiments import promote_params

        return promote_params(kwargs.pop("job_id"), **kwargs)
    raise ValueError(f"unknown op: {op}")


def main() -> None:
    request = json.load(sys.stdin)
    result = _run(request["op"], dict(request.get("kwargs") or {}))
    json.dump(result, sys.stdout, default=str)


if __name__ == "__main__":
    main()
