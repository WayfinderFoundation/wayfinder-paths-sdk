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

# How the "add job" action appears on each surface. Both forms are used by
# session-message discovery to find the chat that registered a job.
ADD_JOB_CLI_VERB: Final[str] = "add-job"  # CLI command name (Click convention)
ADD_JOB_MCP_ACTION: Final[str] = "add_job"  # MCP action key (JSON identifier)

# Control protocol limits
MAX_LINE_BYTES: Final[int] = 1024 * 1024
