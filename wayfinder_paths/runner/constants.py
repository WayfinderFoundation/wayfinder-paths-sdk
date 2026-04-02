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

# Command identifiers — used in CLI, MCP, and session discovery
ADD_JOB_CLI_VERB: Final[str] = "add-job"
ADD_JOB_MCP_ACTION: Final[str] = "add_job"

# Control protocol limits
MAX_LINE_BYTES: Final[int] = 1024 * 1024
