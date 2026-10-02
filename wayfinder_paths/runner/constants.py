from __future__ import annotations

from enum import StrEnum
from typing import Final


class JobStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    ERROR = "ERROR"


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    OK = "OK"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    ABORTED = "ABORTED"


# Supported job types
JOB_TYPE_STRATEGY: Final[str] = "strategy"
JOB_TYPE_SCRIPT: Final[str] = "script"

ADD_JOB_CLI_VERB: Final[str] = "add-job"  # CLI command name (Click convention)
RUNNER_SESSION_ACTIONS: Final[frozenset[str]] = frozenset(
    {"add_job", "update_job", "resume_job", "run_once"}
)

# Control protocol limits
MAX_LINE_BYTES: Final[int] = 1024 * 1024
