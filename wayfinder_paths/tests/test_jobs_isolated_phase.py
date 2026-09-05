from __future__ import annotations

import json
import multiprocessing
import os
import time
from pathlib import Path

import pytest

import wayfinder_paths.jobs.isolated_phase as isolated_phase
from wayfinder_paths.jobs.failures import TransientInfrastructureError
from wayfinder_paths.jobs.isolated_phase import run_isolated_phase


def _child_payload(value: int) -> dict[str, int]:
    return {"value": value, "pid": os.getpid()}


def _child_failure() -> dict[str, int]:
    raise ValueError("candidate is invalid")


def _child_transient_failure() -> dict[str, int]:
    raise TransientInfrastructureError("box is saturated")


def _child_contract_failure_worded_like_infrastructure() -> dict[str, int]:
    raise ValueError(
        "window-invariance probe failed: carry long memory as incremental state"
    )


def _child_crash_worded_like_infrastructure() -> dict[str, int]:
    raise OSError("connection reset while reading bars")


def _sleeping_child(seconds: float) -> dict[str, bool]:
    time.sleep(seconds)
    return {"complete": True}


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


def test_candidate_contract_failure_stays_evidence_whatever_its_wording() -> None:
    # The probe's hint mentions "memory"; the string classifier alone would
    # call that infrastructure and the finalize would die on the candidate.
    with pytest.raises(RuntimeError, match="window-invariance probe failed"):
        run_isolated_phase(
            _child_contract_failure_worded_like_infrastructure, timeout_s=10
        )
    with pytest.raises(TransientInfrastructureError, match="connection reset"):
        run_isolated_phase(_child_crash_worded_like_infrastructure, timeout_s=10)


def test_heavy_child_registration_is_atomic_and_pid_specific(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WAYFINDER_HEAVY_OP_REGISTRY_DIR", str(tmp_path))
    monkeypatch.setattr(isolated_phase, "pid_is_op_runner", lambda _pid: True)
    monkeypatch.setattr(isolated_phase, "proc_start_ticks", lambda pid: pid + 100)

    path = isolated_phase._register_starting_supervisor()
    assert path is not None
    isolated_phase._update_registration(path, child_pid=321)

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["state"] == "running"
    assert record["supervisor_pid"] == os.getpid()
    assert record["child_pid"] == 321
    assert record["child_start_ticks"] == 421
    assert path.stat().st_mode & 0o777 == 0o600
    isolated_phase._remove_registration(path)
    assert not path.exists()


def test_governor_pause_time_does_not_consume_phase_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("pause supervision requires fork")
    started = time.monotonic()
    monkeypatch.setattr(
        isolated_phase,
        "_governor_pause_state",
        lambda _pid: time.monotonic() - started < 1.2,
    )

    result = run_isolated_phase(_sleeping_child, 1.4, timeout_s=0.5)

    assert result == {"complete": True}


def test_stale_governor_cannot_strand_a_paused_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("pause supervision requires fork")
    calls = 0

    def pause_then_stale(_pid: int | None) -> bool | None:
        nonlocal calls
        calls += 1
        return True if calls == 1 else None

    monkeypatch.setattr(isolated_phase, "_governor_pause_state", pause_then_stale)
    monkeypatch.setattr(isolated_phase, "GOVERNOR_STATE_MAX_AGE_SECONDS", 0.1)

    with pytest.raises(TransientInfrastructureError, match="went stale"):
        run_isolated_phase(_sleeping_child, 5.0, timeout_s=10)
