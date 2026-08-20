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

import shutil
import time
from pathlib import Path


class TransientInfrastructureError(RuntimeError):
    """A box condition (OOM/lock/timeout) interrupted a pipeline step.

    Raised instead of recording a failed artifact so transient box state
    never freezes into durable evidence: callers abort and retry when the
    box is quiet rather than staging a "failed" report the approve gate
    reads forever.
    """


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
    # `timeout`-killed subprocesses: under CPU steal a local CLI dies at its
    # deadline with exit 124 — box starvation, never a remote outage.
    "exit 124",
    "exit status 124",
    "command timed out",
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


def disk_used_pct(path: str | Path) -> float | None:
    """Percent of the filesystem holding `path` in use; None on failure.

    The jobs box's 2GB /wf volume silently filled to 100%: boot rsync died
    half-way, opencode serve crash-looped, runnerd could not start, and live
    trading loops went dark ~25 minutes with zero alerting. Fill level is
    the box truth that predicts that failure mode before it lands. Never
    raises.
    """
    try:
        usage = shutil.disk_usage(path)
        if usage.total <= 0:
            return None
        return 100.0 * usage.used / usage.total
    except OSError:  # missing path / unmounted volume
        return None


def cpu_steal_pct(sample_seconds: float = 0.2) -> float | None:
    """CPU steal share (%) over a short /proc/stat sample; None off-Linux.

    On a shared-CPU box the hypervisor's steal is the single number that
    predicts whether local subprocesses will crawl (81-93% steal turned
    ~90s SDK cold-imports into OOM-killed multi-minute hangs). Two reads
    ~sample_seconds apart, steal delta over total delta. Never raises.
    """
    try:

        def _sample() -> tuple[int, int]:
            first = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0]
            values = [int(v) for v in first.split()[1:]]
            steal = values[7] if len(values) > 7 else 0
            return steal, sum(values)

        steal_a, total_a = _sample()
        time.sleep(sample_seconds)
        steal_b, total_b = _sample()
        total_delta = total_b - total_a
        if total_delta <= 0:
            return None
        return 100.0 * (steal_b - steal_a) / total_delta
    except Exception:  # noqa: BLE001 — non-Linux boxes have no /proc/stat
        return None
