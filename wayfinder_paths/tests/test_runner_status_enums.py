from __future__ import annotations

import json

from wayfinder_paths.runner.constants import JobStatus, RunStatus


def test_job_status_is_str_enum() -> None:
    assert str(JobStatus.ACTIVE) == "ACTIVE"
    assert JobStatus("ACTIVE") is JobStatus.ACTIVE
    assert json.dumps({"status": JobStatus.PAUSED}) == '{"status": "PAUSED"}'


def test_run_status_is_str_enum() -> None:
    assert str(RunStatus.ABORTED) == "ABORTED"
    assert RunStatus("OK") is RunStatus.OK
    assert json.dumps({"status": RunStatus.TIMEOUT}) == '{"status": "TIMEOUT"}'
