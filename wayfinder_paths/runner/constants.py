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

# Command identifier — used in CLI and session discovery
ADD_JOB_VERB: Final[str] = "add-job"

# Control protocol limits
MAX_LINE_BYTES: Final[int] = 1024 * 1024
