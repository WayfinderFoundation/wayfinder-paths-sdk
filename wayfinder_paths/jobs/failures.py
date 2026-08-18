"""Failure taxonomy: infrastructure vs evidence.

One OOM at 21:04Z (live production) cascaded into 14 hours of frozen
research: the job-worker agent recorded "OOM-blocked" in its agendas, marked
research lanes "exhausted", and self-rejected an owner-approved proposal
whose mechanical re-stage failed inside the OOM window. Every recovery
required a human operator. The missing primitive was a shared, dumb answer
to one question: is this failure about the BOX or about the EVIDENCE?

The asymmetry rule: INFRASTRUCTURE failures (OOM, locks, timeouts, dead
event loops, unreachable services) are transient box conditions — they are
watchdog-retried, self-repair mechanically, and may never bury approved
work or close a research lane. EVIDENCE failures (failed validation, red
gates, refuted hypotheses) still stop the line — that is the system working.
"""

from __future__ import annotations

# Deliberately dumb and greppable: case-insensitive substring match against a
# flat list. No regex cleverness — anyone auditing a recovery decision should
# be able to grep the error text against this list by eye.
INFRASTRUCTURE_PATTERNS = (
    "oom",
    "out of memory",
    "memory",
    "killed",
    "lock busy",
    "heavy-compute lock",
    "timeout",
    "timed out",
    "408",
    "429",
    "502",
    "503",
    "event loop is closed",
    "connection",
    "opencode-unavailable",
    "prompt_async",
    "resource temporarily unavailable",
)


def classify_failure(error: str) -> str:
    """Classify an error string as "infrastructure" or "evidence"."""
    text = (error or "").lower()
    if any(pattern in text for pattern in INFRASTRUCTURE_PATTERNS):
        return "infrastructure"
    return "evidence"
