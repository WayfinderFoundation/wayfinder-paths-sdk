"""Estimate the machine's shared-CPU burst-credit balance so the runner can
postpone background jobs *before* the balance hits zero and the whole machine
gets throttled to its baseline share (after which every request — including the
interactive agent — crawls).

A shared-CPU machine gets a small guaranteed baseline (~1/16 core per vCPU) and
banks unused time as burst credits (capped). Sustained CPU above baseline drains
the credits; at zero the machine is pinned at baseline. The guest can't read the
hypervisor's real credit counter, so this integrates ``baseline - observed_burn``
over time from ``/proc/stat`` — a deliberately conservative proxy.

Off the target platform (no readable ``/proc/stat``) it disables itself and
never reports "over quota", so gating on it is a safe no-op there.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable

# Fly shared-cpu baseline: 6.25% of a core per vCPU.
BASELINE_CORES_PER_VCPU = 0.0625


def _read_busy_jiffies() -> int | None:
    """Aggregate busy CPU jiffies across all cores (user+nice+system+irq+softirq)
    from the first line of /proc/stat. None if unreadable."""
    try:
        with open("/proc/stat") as f:
            fields = [int(x) for x in f.readline().split()[1:]]
        # user nice system idle iowait irq softirq ...
        return fields[0] + fields[1] + fields[2] + fields[5] + fields[6]
    except (OSError, ValueError, IndexError):
        return None


class BurstEstimator:
    def __init__(
        self,
        *,
        cap_cpu_s: float,
        low_water_cpu_s: float,
        baseline_cores: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        vcpus = os.cpu_count() or 1
        self._baseline = (
            baseline_cores if baseline_cores is not None else BASELINE_CORES_PER_VCPU * vcpus
        )
        self._cap = float(cap_cpu_s)
        self._low_water = float(low_water_cpu_s)
        self._clock = clock
        self._hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        self._balance = float(cap_cpu_s)  # optimistic start; converges within minutes
        self._last_jiffies = _read_busy_jiffies()
        self._last_t = clock()
        # Disabled when /proc/stat can't be read (non-Linux, sandbox) → no-op.
        self._enabled = self._last_jiffies is not None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def balance(self) -> float:
        return self._balance

    def update(self) -> None:
        """Advance the estimate. Call once per tick."""
        if not self._enabled:
            return
        jiffies = _read_busy_jiffies()
        now = self._clock()
        dt = now - self._last_t
        if jiffies is None or self._last_jiffies is None or dt <= 0:
            self._last_jiffies, self._last_t = jiffies, now
            return
        burn = (jiffies - self._last_jiffies) / self._hz / dt  # core-equivalents
        self._balance = max(0.0, min(self._cap, self._balance + (self._baseline - burn) * dt))
        self._last_jiffies, self._last_t = jiffies, now

    def over_quota(self) -> bool:
        """True when the estimated balance is low enough that launching more
        background work risks pinning the machine. Always False when disabled."""
        return self._enabled and self._balance < self._low_water
