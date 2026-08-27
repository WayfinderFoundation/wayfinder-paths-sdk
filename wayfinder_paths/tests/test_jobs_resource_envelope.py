from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from wayfinder_paths.jobs.failures import TransientInfrastructureError
from wayfinder_paths.jobs.resource_envelope import (
    evolution_launch_readiness,
    require_evolution_launch_headroom,
)


def _write_state(path, *, now: datetime, **overrides) -> None:
    state = {
        "updated_at": now.timestamp(),
        "balance_pct": 80.0,
        "paused": False,
        "allow_new_heavy": True,
        **overrides,
    }
    path.write_text(json.dumps(state), encoding="utf-8")


def test_launch_readiness_uses_fresh_governor_state(tmp_path, monkeypatch) -> None:
    now = datetime.now(UTC)
    path = tmp_path / "burst.json"
    monkeypatch.setenv("WAYFINDER_BURST_STATE_PATH", str(path))
    _write_state(
        path,
        now=now,
        balance_pct=12.0,
        paused=True,
        allow_new_heavy=False,
    )

    readiness = evolution_launch_readiness(now=now)

    assert readiness["ready"] is False
    assert readiness["balance_pct"] == 12.0
    with pytest.raises(TransientInfrastructureError, match="burst reserve"):
        require_evolution_launch_headroom()


def test_launch_readiness_fails_closed_on_stale_governor_state(
    tmp_path, monkeypatch
) -> None:
    now = datetime.now(UTC)
    path = tmp_path / "burst.json"
    monkeypatch.setenv("WAYFINDER_BURST_STATE_PATH", str(path))
    _write_state(path, now=now - timedelta(minutes=1))

    readiness = evolution_launch_readiness(now=now)

    assert readiness["ready"] is False
    assert readiness["source"] == "governor_stale"


def test_launch_readiness_is_portable_without_a_governor(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WAYFINDER_BURST_STATE_PATH", str(tmp_path / "missing.json"))

    assert evolution_launch_readiness()["ready"] is True
