"""Every Watchdog panel line is computed by the function the runtime uses.

Parity here means: for each input the panel and the runtime both read, the
panel shows exactly what the runtime would do.

(a) Kill switches: ``watchdog_view(...)["kill_switches"]`` equals the numeric
    limits of the ``RiskLimits`` object ``check_risk_halt`` checks each tick
    (captured at its ``check`` call), across job risk_limits.json files ×
    owner governance files. No limits object means no kill switches.
(b) Native stops: for a live entry whose bracket carries a stop, the engine's
    ``native_protection_skip_reason`` returns non-None exactly when
    ``risk_flags`` raises ``no_native_stop`` — ``warn`` for ``opted_out``,
    ``info`` for ``unsupported_broker`` — across
    ``native_stop_required`` ∈ {unset, True, False} × a venue whose live
    broker places venue stops (hyperliquid) and one whose broker cannot
    (hyperliquid_spot). The risk flag reads venue capabilities and the engine
    inspects the broker, so the test also pins that the two agree per venue.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from wayfinder_paths.core.strategies.risk_limits import RiskLimits
from wayfinder_paths.jobs.execution.engine import (
    EngineState,
    native_protection_skip_reason,
)
from wayfinder_paths.jobs.execution.hyperliquid import HyperliquidPerpBroker
from wayfinder_paths.jobs.execution.hyperliquid_spot import HyperliquidSpotBroker
from wayfinder_paths.jobs.execution.risk import check_risk_halt
from wayfinder_paths.jobs.execution.venues import (
    NativeProtectionBroker,
    venue_capabilities,
)
from wayfinder_paths.jobs.launch import KILL_SWITCH_KEYS, watchdog_view
from wayfinder_paths.jobs.risk_flags import risk_flags
from wayfinder_paths.tests.test_jobs_live_driver import _make_job, _now, _view
from wayfinder_paths.tests.test_jobs_risk_limits import (
    _write_governance,
    _write_limits,
)

JOB_LIMITS: list[dict[str, Any] | None] = [
    None,
    {},
    {"max_drawdown": -0.05, "max_daily_loss_usd": 25.0},
    {
        "max_drawdown": -0.5,
        "max_gross_exposure_usd": 50_000.0,
        "max_position_per_symbol_usd": 1_000.0,
        "pause_after_consecutive_losses": 3,
    },
    {"max_gross_exposure_usd": 5_000.0, "min_rolling_30d_sharpe": 0.5},
]
GOVERNANCE: list[dict[str, Any] | None] = [
    None,
    {"max_drawdown": 0.15},
    {
        "max_drawdown": 0.15,
        "max_drawdown_pct": 0.15,
        "max_tail_loss": 0.1,
        "max_leverage": 3.0,
    },
    {"max_gross_exposure": 20_000.0},
    {"max_gross_exposure_usd": 100_000.0, "max_drawdown": -0.3},
]


@pytest.mark.parametrize("governance", GOVERNANCE)
@pytest.mark.parametrize("job_limits", JOB_LIMITS)
def test_kill_switches_are_the_limits_the_risk_halt_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    job_limits: dict[str, Any] | None,
    governance: dict[str, Any] | None,
) -> None:
    store, job, root = _make_job(tmp_path, params={"initial_capital": 10_000.0})
    if job_limits is not None:
        _write_limits(root, job_limits)
    if governance is not None:
        _write_governance(tmp_path, job.id, governance)
    checked: list[RiskLimits] = []

    def capture(self: RiskLimits, snapshot: dict[str, Any]) -> str | None:
        checked.append(self)
        return None

    monkeypatch.setattr(RiskLimits, "check", capture)
    view = _view(2)
    check_risk_halt(
        root,
        state=EngineState(),
        view=view,
        params=job.execution_params,
        now=_now(view),
    )

    enforced = (
        {
            key: getattr(checked[0], key)
            for key in KILL_SWITCH_KEYS
            if getattr(checked[0], key) is not None
        }
        if checked
        else {}
    )
    panel = watchdog_view(store.load(job.id), root)
    assert panel["kill_switches"] == enforced
    assert set(panel["kill_switch_sources"]) == set(enforced)
    for key, value in enforced.items():
        from_job = (job_limits or {}).get(key)
        expected = "job" if from_job is not None and from_job == value else "owner"
        assert panel["kill_switch_sources"][key] == expected
    assert "max_drawdown_pct" not in panel["kill_switches"]
    assert "max_tail_loss" not in panel["kill_switches"]


VENUE_BROKERS = {
    "hyperliquid": HyperliquidPerpBroker,
    "hyperliquid_spot": HyperliquidSpotBroker,
}
SKIP_REASON_SEVERITY = {"opted_out": "warn", "unsupported_broker": "info"}


@pytest.mark.parametrize("venue", sorted(VENUE_BROKERS))
@pytest.mark.parametrize("pinned", [None, True, False])
def test_no_native_stop_flag_fires_iff_the_engine_skips_the_venue_stop(
    tmp_path: Path, venue: str, pinned: bool | None
) -> None:
    # Uninitialised on purpose: protocol membership is decided by the class's
    # methods, and building a live broker needs a wallet.
    broker = object.__new__(VENUE_BROKERS[venue])
    assert venue_capabilities(venue).supports_brackets == isinstance(
        broker, NativeProtectionBroker
    )

    store, job, root = _make_job(tmp_path)
    job.execution_spec = {**job.execution_spec, "venues": [venue]}
    if pinned is not None:
        job.execution_params["native_stop_required"] = pinned
    store.save(job)

    skip_reason = native_protection_skip_reason(
        {"stop_loss": 90.0}, job.execution_params, broker
    )
    flags = {flag["code"]: flag for flag in risk_flags(store.load(job.id), root)}

    if skip_reason is None:
        assert "no_native_stop" not in flags
    else:
        assert flags["no_native_stop"]["severity"] == SKIP_REASON_SEVERITY[skip_reason]


def test_watchdog_view_payload_stays_numeric_for_the_panel(tmp_path: Path) -> None:
    store, job, root = _make_job(tmp_path)
    _write_limits(root, {"max_daily_loss_usd": 25.0})
    _write_governance(tmp_path, job.id, {"max_drawdown": 0.15, "max_leverage": 3})
    panel = watchdog_view(store.load(job.id), root)
    assert json.loads(json.dumps(panel["kill_switches"])) == {
        "max_daily_loss_usd": 25.0,
        "max_drawdown": -0.15,
    }
    assert panel["kill_switch_sources"] == {
        "max_daily_loss_usd": "job",
        "max_drawdown": "owner",
    }
    assert panel["leverage_ceiling"] == {"value": 3.0, "source": "owner"}
