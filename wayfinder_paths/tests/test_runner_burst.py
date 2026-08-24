from __future__ import annotations

import wayfinder_paths.runner.burst as burst_mod
from wayfinder_paths.runner.burst import BurstEstimator


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _estimator(monkeypatch, jiffies_seq, clock, *, cap=100.0, low=20.0, baseline=0.1):
    """BurstEstimator whose /proc/stat reads come from jiffies_seq (a list the
    test pops from) so we can drive burn precisely. HZ pinned to 100."""
    seq = list(jiffies_seq)

    def fake_read():
        return seq.pop(0) if seq else seq_last[0]

    seq_last = [jiffies_seq[-1]]
    monkeypatch.setattr(burst_mod, "_read_busy_jiffies", fake_read)
    est = BurstEstimator(
        cap_cpu_s=cap, low_water_cpu_s=low, baseline_cores=baseline, clock=clock
    )
    return est


def test_high_burn_drains_to_zero(monkeypatch):
    clock = _Clock()
    # 1 full core busy per second (100 jiffies/s at HZ=100) vs baseline 0.1 core.
    # Net drain ~0.9 CPU-s/s; from cap=100 → ~111s to hit 0, definitely <0 in 200s.
    jiffies = [0] + [100 * i for i in range(1, 300)]
    est = _estimator(monkeypatch, jiffies, clock, cap=100.0, low=20.0, baseline=0.1)
    for i in range(1, 250):
        clock.t = float(i)
        est.update()
    assert est.balance == 0.0
    assert est.over_quota() is True


def test_idle_reaccrues_and_clamps_to_cap(monkeypatch):
    clock = _Clock()
    # Zero burn (jiffies flat) → balance climbs by baseline each second, clamps at cap.
    jiffies = [0] * 300
    est = _estimator(monkeypatch, jiffies, clock, cap=100.0, low=20.0, baseline=0.1)
    est._balance = 0.0  # start pinned
    for i in range(1, 250):
        clock.t = float(i)
        est.update()
    assert est.balance <= 100.0
    assert est.balance > 0.0  # recovered off the pin
    assert est.over_quota() is False


def test_over_quota_threshold(monkeypatch):
    clock = _Clock()
    est = _estimator(monkeypatch, [0, 0], clock, cap=100.0, low=20.0, baseline=0.1)
    est._balance = 25.0
    assert est.over_quota() is False
    est._balance = 15.0
    assert est.over_quota() is True


def test_disabled_when_proc_unreadable(monkeypatch):
    monkeypatch.setattr(burst_mod, "_read_busy_jiffies", lambda: None)
    est = BurstEstimator(cap_cpu_s=100.0, low_water_cpu_s=20.0, baseline_cores=0.1)
    est._balance = 0.0
    est.update()  # no-op
    assert est.enabled is False
    assert est.over_quota() is False  # never gates off-platform
