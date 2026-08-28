"""Consume the image CPU-budget governor, with a conservative local fallback.

On hosted shared-CPU Machines the shell governor is authoritative: it anchors
its local integrator to Fly telemetry delivered through the authenticated jobs
sync and publishes a small state file.  The runner only estimates locally when
that state is absent or stale, so upgrades remain backward compatible.
"""

from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wayfinder_paths.runner.monitor_state import atomic_write_json

BASELINE_CORES_PER_VCPU = 0.0625
INITIAL_BALANCE_CPU_S_PER_VCPU = 5.0
MAX_BALANCE_CPU_S_PER_VCPU = 500.0
GOVERNOR_STATE_PATH = Path("/tmp/wayfinder-burst-governor.json")
CPU_BUDGET_ANCHOR_PATH = Path("/tmp/wayfinder-fly-cpu-anchor.json")
GOVERNOR_STATE_MAX_AGE_SECONDS = 10.0


def _read_busy_jiffies() -> int | None:
    """Return aggregate busy CPU jiffies, or ``None`` off Linux."""
    try:
        with open("/proc/stat") as handle:
            fields = [int(value) for value in handle.readline().split()[1:]]
        return fields[0] + fields[1] + fields[2] + fields[5] + fields[6]
    except (OSError, ValueError, IndexError):
        return None


class BurstEstimator:
    """Guest-only estimate used when the image governor is unavailable."""

    def __init__(
        self,
        *,
        cap_cpu_s: float,
        low_water_cpu_s: float,
        baseline_cores: float | None = None,
        initial_balance_cpu_s: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        vcpus = os.cpu_count() or 1
        self._baseline = (
            baseline_cores
            if baseline_cores is not None
            else BASELINE_CORES_PER_VCPU * vcpus
        )
        self._cap = float(cap_cpu_s)
        self._low_water = float(low_water_cpu_s)
        self._clock = clock
        self._hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        initial = (
            INITIAL_BALANCE_CPU_S_PER_VCPU * vcpus
            if initial_balance_cpu_s is None
            else initial_balance_cpu_s
        )
        self._balance = min(self._cap, max(0.0, float(initial)))
        self._last_jiffies = _read_busy_jiffies()
        self._last_t = clock()
        self._enabled = self._last_jiffies is not None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def balance(self) -> float:
        return self._balance

    @property
    def capacity(self) -> float:
        return self._cap

    def update(self) -> None:
        if not self._enabled:
            return
        jiffies = _read_busy_jiffies()
        now = self._clock()
        elapsed = now - self._last_t
        if jiffies is None or self._last_jiffies is None or elapsed <= 0:
            self._last_jiffies, self._last_t = jiffies, now
            return
        used_cores = (jiffies - self._last_jiffies) / self._hz / elapsed
        self._balance = max(
            0.0,
            min(
                self._cap,
                self._balance + (self._baseline - used_cores) * elapsed,
            ),
        )
        self._last_jiffies, self._last_t = jiffies, now

    def over_quota(self) -> bool:
        return self._enabled and self._balance < self._low_water


def write_cpu_budget_anchor(
    payload: Any,
    *,
    path: Path = CPU_BUDGET_ANCHOR_PATH,
    received_at: float | None = None,
) -> bool:
    """Validate and atomically publish the backend's narrow Fly projection."""
    if not isinstance(payload, dict):
        return False
    try:
        balance = float(payload["balance_cpu_seconds"])
        throttle = float(payload["throttle_total_seconds"])
        baseline = float(payload["baseline_cores"])
        observed_text = str(payload["observed_at"])
        observed = datetime.fromisoformat(observed_text)
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=UTC)
        observed_timestamp = observed.timestamp()
        received_timestamp = time.time() if received_at is None else float(received_at)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    if not all(
        math.isfinite(value)
        for value in (
            balance,
            throttle,
            baseline,
            observed_timestamp,
            received_timestamp,
        )
    ):
        return False
    if balance < 0 or throttle < 0 or baseline <= 0:
        return False
    atomic_write_json(
        path,
        {
            "schema_version": "1.0",
            "balance_cpu_seconds": balance,
            "throttle_total_seconds": throttle,
            "baseline_cores": baseline,
            "observed_at": observed.isoformat(),
            "received_at": received_timestamp,
        },
    )
    os.chmod(path, 0o600)
    return True


class BurstBudget:
    """Runner admission view over fresh governor state or local estimation."""

    def __init__(
        self,
        fallback: BurstEstimator,
        *,
        state_path: Path = GOVERNOR_STATE_PATH,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._fallback = fallback
        self._state_path = state_path
        self._wall_clock = wall_clock

    def _state(self) -> tuple[dict[str, Any], float] | None:
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
            age = max(0.0, self._wall_clock() - float(state["updated_at"]))
        except (OSError, KeyError, TypeError, ValueError):
            return None
        if not isinstance(state, dict) or age > GOVERNOR_STATE_MAX_AGE_SECONDS:
            return None
        return state, age

    @staticmethod
    def _governor_balance(state: dict[str, Any], capacity: float) -> float:
        raw = state.get("balance_cpu_seconds")
        if isinstance(raw, (int, float)) and math.isfinite(float(raw)):
            return max(0.0, float(raw))
        percent = state.get("balance_pct")
        if isinstance(percent, (int, float)) and math.isfinite(float(percent)):
            return max(0.0, float(percent)) / 100.0 * capacity
        return 0.0

    @property
    def balance(self) -> float:
        current = self._state()
        if current is None:
            return self._fallback.balance
        state, _ = current
        capacity = float(state.get("capacity_cpu_seconds") or self._fallback.capacity)
        return self._governor_balance(state, capacity)

    def update(self) -> None:
        if self._state() is None:
            self._fallback.update()

    def over_quota(self) -> bool:
        current = self._state()
        if current is None:
            return self._fallback.over_quota()
        state, _ = current
        return bool(state.get("paused")) or state.get("allow_new_heavy") is not True

    def snapshot(self) -> dict[str, Any]:
        current = self._state()
        if current is None:
            capacity = self._fallback.capacity
            return {
                "source": "local_estimator" if self._fallback.enabled else "disabled",
                "balance_cpu_seconds": round(self._fallback.balance, 3),
                "capacity_cpu_seconds": capacity,
                "balance_pct": round(self._fallback.balance / capacity * 100, 2)
                if capacity > 0
                else None,
                "allow_new_heavy": not self._fallback.over_quota(),
            }
        state, age = current
        capacity = float(state.get("capacity_cpu_seconds") or self._fallback.capacity)
        result = {
            key: state.get(key)
            for key in (
                "schema_version",
                "source",
                "balance_pct",
                "allow_new_heavy",
                "budget_low",
                "paused",
                "affected_pids",
                "anchor_age_seconds",
                "throttle_total_seconds",
            )
        }
        result.update(
            {
                "state_age_seconds": round(age, 2),
                "balance_cpu_seconds": round(
                    self._governor_balance(state, capacity), 3
                ),
                "capacity_cpu_seconds": capacity,
            }
        )
        return result
