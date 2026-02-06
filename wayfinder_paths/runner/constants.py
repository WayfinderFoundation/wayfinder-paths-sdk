from __future__ import annotations

from typing import Final

# Job status values
JOB_STATUS_ACTIVE: Final[str] = "ACTIVE"
JOB_STATUS_PAUSED: Final[str] = "PAUSED"
JOB_STATUS_ERROR: Final[str] = "ERROR"

# Run status values
RUN_STATUS_RUNNING: Final[str] = "RUNNING"
RUN_STATUS_OK: Final[str] = "OK"
RUN_STATUS_FAILED: Final[str] = "FAILED"
RUN_STATUS_TIMEOUT: Final[str] = "TIMEOUT"
RUN_STATUS_ABORTED: Final[str] = "ABORTED"

# Supported job types
JOB_TYPE_STRATEGY: Final[str] = "strategy"
JOB_TYPE_SCRIPT: Final[str] = "script"
