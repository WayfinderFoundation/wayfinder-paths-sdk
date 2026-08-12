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

# ERROR lanes retry after max(4 * interval, this floor) instead of parking
# until a human resumes them — ERROR means "backing off", not "dead".
ERROR_RETRY_COOLDOWN_SECONDS: Final[int] = 1800

# How the "add job" action appears on each surface. Both forms are used by
# session-message discovery to find the chat that registered a job.
ADD_JOB_CLI_VERB: Final[str] = "add-job"  # CLI command name (Click convention)
ADD_JOB_MCP_ACTION: Final[str] = "add_job"  # MCP action key (JSON identifier)

# Control protocol limits
MAX_LINE_BYTES: Final[int] = 1024 * 1024

# Concurrent worker cap. Each worker is a full SDK Python (~170MB
# baseline, several hundred MB during wake-path backtests/sims). On the
# 2GB boxes, 4 concurrent workers atop opencode+MCP+runnerd blew through
# memory during job-start bursts and the OOM killer took runnerd twice
# (2026-07-27, 2026-07-31 — silent mid-line log stops). Two workers
# halve the burst ceiling; ticks are seconds-long so queueing is cheap.
DEFAULT_MAX_WORKERS = 2
