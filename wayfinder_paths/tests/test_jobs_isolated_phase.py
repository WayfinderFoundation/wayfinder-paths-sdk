from __future__ import annotations

import multiprocessing
import os

import pytest

from wayfinder_paths.jobs.failures import TransientInfrastructureError
from wayfinder_paths.jobs.isolated_phase import run_isolated_phase


def _child_payload(value: int) -> dict[str, int]:
    return {"value": value, "pid": os.getpid()}


def _child_failure() -> dict[str, int]:
    raise ValueError("candidate is invalid")


def _child_transient_failure() -> dict[str, int]:
    raise TransientInfrastructureError("box is saturated")


def test_isolated_phase_returns_compact_result_from_disposable_child() -> None:
    result = run_isolated_phase(_child_payload, 7, timeout_s=10)

    assert result["value"] == 7
    if "fork" in multiprocessing.get_all_start_methods():
        assert result["pid"] != os.getpid()


def test_isolated_phase_preserves_candidate_failure_as_evidence() -> None:
    with pytest.raises(RuntimeError, match="candidate is invalid"):
        run_isolated_phase(_child_failure, timeout_s=10)


def test_isolated_phase_preserves_transient_failure_for_retry() -> None:
    with pytest.raises(TransientInfrastructureError, match="box is saturated"):
        run_isolated_phase(_child_transient_failure, timeout_s=10)
