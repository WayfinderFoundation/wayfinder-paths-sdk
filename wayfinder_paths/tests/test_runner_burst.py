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


def test_agent_wake_tier_classification():
    from wayfinder_paths.runner.daemon import (
        BURST_SHORT_POSTPONE_S,
        _burst_postpone_tier,
    )

    wake = {"payload": {"env": {"WAYFINDER_JOB_AGENT_MODE": "intervene"}}}
    # Scheduled occurrences of this tier are skipped outright under drain
    # (asserted below); event-triggered ones postpone on the short floor.
    assert _burst_postpone_tier(wake) == ("agent", BURST_SHORT_POSTPONE_S)

    paper = {
        "payload": {
            "env": {
                "WAYFINDER_JOB_EXECUTION_CONTRACT": "jobs_v1",
                "WAYFINDER_JOB_MODE": "paper",
            }
        }
    }
    assert _burst_postpone_tier(paper) == ("script-short", BURST_SHORT_POSTPONE_S)

    live = {
        "payload": {
            "env": {
                "WAYFINDER_JOB_EXECUTION_CONTRACT": "jobs_v1",
                "WAYFINDER_JOB_MODE": "live",
            }
        }
    }
    assert _burst_postpone_tier(live) == ("live-exempt", None)


def test_scheduled_agent_wake_skipped_under_drain(tmp_path, monkeypatch):
    """A scheduled wake due while over quota is dropped for the occurrence:
    schedule advances, no run reserved. Event-triggered wakes still launch
    through the postpone path instead of being skipped."""
    from wayfinder_paths.runner import daemon as daemon_mod

    calls = {"advanced": None, "reserved": 0}

    class FakeBurst:
        balance = 0.0

        def over_quota(self):
            return True

    class FakeDB:
        def set_next_run_at(self, *, job_id, next_run_at):
            calls["advanced"] = (job_id, next_run_at)

        def reserve_run(self, **kwargs):
            calls["reserved"] += 1
            return 1

    daemon = daemon_mod.RunnerDaemon.__new__(daemon_mod.RunnerDaemon)
    daemon._burst = FakeBurst()
    daemon._db = FakeDB()
    daemon._running = []
    daemon._max_workers = 4
    daemon._postponed_since = {}

    monkeypatch.setattr(daemon_mod, "next_run_after", lambda schedule, now: now + 3600)
    monkeypatch.setattr(daemon_mod, "schedule_from_job", lambda job: object())

    job = {
        "id": 7,
        "name": "wake-job",
        "payload": {"env": {"WAYFINDER_JOB_AGENT_MODE": "intervene"}},
    }
    result = daemon._maybe_start_job(job=job, now=1000, reason="schedule")
    assert result is None
    assert calls["advanced"] == (7, 4600)
    assert calls["reserved"] == 0
