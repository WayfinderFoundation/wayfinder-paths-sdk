from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from wayfinder_paths.paths.participation import (
    ParticipationConfig,
    ParticipationState,
    Receipt,
    WorkItem,
    participation_tick,
)

NOW = 1_790_000_000.0


def config(**overrides: Any) -> ParticipationConfig:
    return ParticipationConfig.model_validate(
        {
            "protocol": "flop",
            "program": "testnet",
            "rule_revision": "v1",
            "account": "fixture-only",
            "enabled": True,
            "cost_unit": "FLOP_TEST",
            "max_total_cost": 10,
            "max_daily_cost": 5,
            "max_operation_cost": 2,
            "max_daily_operations": 2,
            "work": [
                {
                    "id": "public-doc-summary-1",
                    "kind": "inference",
                    "max_cost": 2,
                    "request": {
                        "public_input": "Summarize public documentation",
                        "model_hash": "a" * 64,
                        "purpose": "Public documentation summary",
                        "max_output_tokens": 512,
                        "max_latency_seconds": 30,
                        "acceptance_criteria": "Summary cites its public input and includes limitations",
                    },
                }
            ],
            **overrides,
        }
    )


class FixturePort:
    """Test-only receipt simulator. Never imported by the installed Path."""

    supports_submit = True

    def __init__(self, cfg: ParticipationConfig):
        self.cfg = cfg
        self.submitted: list[str] = []
        self.reconciled: list[str] = []
        self.protected: list[bool] = []
        self.crash_after_accept = False
        self.safety_ok = True
        self.settled = False
        self.pause: asyncio.Event | None = None
        self.entered = asyncio.Event()
        self.observation = {
            "protocol": cfg.protocol,
            "program": cfg.program,
            "rule_revision": cfg.rule_revision,
            "observed_at": NOW,
            "readiness": "ready",
            "eligible": True,
            "rewards": [],
        }

    async def protect(self, *, dry_run: bool) -> tuple[bool, str]:
        self.protected.append(dry_run)
        return self.safety_ok, "fixture protective exit"

    async def observe(self) -> tuple[bool, dict[str, Any]]:
        self.entered.set()
        if self.pause:
            await self.pause.wait()
        return True, self.observation

    def receipt(self) -> dict[str, Any]:
        return Receipt(
            status="settled" if self.settled else "pending",
            request_id="fixture-request",
            cost=1,
            cost_unit=self.cfg.cost_unit,
            evidence="fixture:protocol-receipt",
            work_accepted=True if self.settled else None,
            qualifying_activity=None,
        ).model_dump()

    async def submit(
        self, item: WorkItem, *, operation_id: str
    ) -> tuple[bool, dict[str, Any]]:
        self.submitted.append(operation_id)
        if self.crash_after_accept:
            raise TimeoutError("fixture: accepted remotely, response lost")
        return True, self.receipt()

    async def reconcile(
        self, *, operation_id: str, request_id: str | None
    ) -> tuple[bool, dict[str, Any]]:
        self.reconciled.append(operation_id)
        return True, self.receipt()


@pytest.mark.asyncio
async def test_flop_restart_reconciles_without_duplicating_or_inventing_rewards(
    tmp_path: Path,
) -> None:
    cfg = config()
    port = FixturePort(cfg)
    port.crash_after_accept = True
    with pytest.raises(TimeoutError):
        await participation_tick(cfg, port, state_dir=tmp_path, now=NOW, dry_run=False)
    state = ParticipationState.model_validate_json(
        (tmp_path / "participation.json").read_text()
    )
    assert len(state.operations) == 1
    assert next(iter(state.operations.values())).receipt is None
    # A different process/adapter instance can recover with the persisted op ID.
    recovered = FixturePort(cfg)
    recovered.settled = True
    result = await participation_tick(
        cfg, recovered, state_dir=tmp_path, now=NOW + 1, dry_run=False
    )
    assert recovered.submitted == []
    assert recovered.reconciled == port.submitted
    assert result["cost_paid"] == 1
    assert result["budget_remaining"] == 9
    assert result["completed_work"] == 1
    assert result["qualifying_activity"] == 0
    assert result["observation"]["rewards"] == []


@pytest.mark.asyncio
async def test_dry_run_never_submits_or_changes_live_ledger(tmp_path: Path) -> None:
    cfg, port = config(), FixturePort(config())
    result = await participation_tick(
        cfg, port, state_dir=tmp_path, now=NOW, dry_run=True
    )
    assert result["status"] == "dry_run"
    assert not port.submitted and port.protected == [True]
    assert not (tmp_path / "participation.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["bad json", "[]", '{"identity": "wrong"}'])
async def test_corrupt_state_fails_closed_but_still_checks_protection(
    tmp_path: Path, body: str
) -> None:
    (tmp_path / "participation.json").write_text(body)
    cfg, port = config(), FixturePort(config())
    result = await participation_tick(
        cfg, port, state_dir=tmp_path, now=NOW, dry_run=False
    )
    assert result["status"] == "blocked"
    assert port.protected == [False] and not port.submitted
    assert (tmp_path / "participation.json").read_text() == body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        {"enabled": False},
        {"max_daily_cost": 1},
        {"max_total_cost": 1},
        {"max_operation_cost": 1},
    ],
)
async def test_budget_and_permission_blocks(
    tmp_path: Path, changed: dict[str, Any]
) -> None:
    cfg = config(**changed)
    port = FixturePort(cfg)
    result = await participation_tick(
        cfg, port, state_dir=tmp_path, now=NOW, dry_run=False
    )
    assert result["status"] == "blocked" and not port.submitted


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        {"eligible": False},
        {"eligible": None},
        {"readiness": "blocked"},
        {"rule_revision": "v2"},
        {"observed_at": NOW - 301},
        {"observed_at": NOW + 600},
    ],
)
async def test_readiness_rules_and_freshness(
    tmp_path: Path, changed: dict[str, Any]
) -> None:
    cfg, port = config(), FixturePort(config())
    port.observation.update(changed)
    result = await participation_tick(
        cfg, port, state_dir=tmp_path, now=NOW, dry_run=False
    )
    assert result["status"] == "blocked" and not port.submitted


@pytest.mark.asyncio
async def test_pending_timeout_latches_and_reserves_budget(tmp_path: Path) -> None:
    cfg, port = config(), FixturePort(config())
    await participation_tick(cfg, port, state_dir=tmp_path, now=NOW, dry_run=False)
    result = await participation_tick(
        cfg, port, state_dir=tmp_path, now=NOW + 601, dry_run=False
    )
    assert result["budget_remaining"] == 8
    assert "deadline" in result["reason"]
    port.settled = True
    result = await participation_tick(
        cfg, port, state_dir=tmp_path, now=NOW + 602, dry_run=False
    )
    assert "deadline" in result["reason"]  # no automatic re-arm
    assert len(port.submitted) == 1


@pytest.mark.asyncio
async def test_cross_coroutine_ticks_cannot_duplicate(tmp_path: Path) -> None:
    cfg, port = config(), FixturePort(config())
    port.pause = asyncio.Event()
    first = asyncio.create_task(
        participation_tick(cfg, port, state_dir=tmp_path, now=NOW, dry_run=False)
    )
    await port.entered.wait()
    second = await participation_tick(
        cfg, port, state_dir=tmp_path, now=NOW, dry_run=False
    )
    assert second["status"] == "busy"
    port.pause.set()
    await first
    assert len(port.submitted) == 1


@pytest.mark.asyncio
async def test_trading_pause_keeps_protection_and_partial_fill_reconciliation(
    tmp_path: Path,
) -> None:
    cfg = config(
        protocol="risex",
        cost_unit="USD",
        work=[
            {
                "id": "hedged-btc",
                "kind": "trade",
                "max_cost": 2,
                "request": {"symbol": "BTC", "fixture_only": True},
            }
        ],
    )
    port = FixturePort(cfg)
    await participation_tick(cfg, port, state_dir=tmp_path, now=NOW, dry_run=False)
    # Pausing activity must still invoke adapter risk/hedge recovery and reconcile
    # partial orders. Actual venue hedge integration is a separate acceptance test.
    cfg.enabled = False
    result = await participation_tick(
        cfg, port, state_dir=tmp_path, now=NOW + 1, dry_run=False
    )
    assert port.protected == [False, False]
    assert port.reconciled == port.submitted
    assert len(port.submitted) == 1 and result["operations"] == 1


@pytest.mark.asyncio
async def test_risk_failure_latches_before_submission(tmp_path: Path) -> None:
    cfg, port = config(), FixturePort(config())
    port.safety_ok = False
    result = await participation_tick(
        cfg, port, state_dir=tmp_path, now=NOW, dry_run=False
    )
    assert "risk protection failed" in result["reason"] and not port.submitted


@pytest.mark.asyncio
async def test_changing_a_submitted_work_item_cannot_double_spend(
    tmp_path: Path,
) -> None:
    cfg, port = config(), FixturePort(config())
    port.settled = True
    await participation_tick(cfg, port, state_dir=tmp_path, now=NOW, dry_run=False)
    cfg.work[0].request["public_input"] = "changed after review"
    result = await participation_tick(
        cfg, port, state_dir=tmp_path, now=NOW + 1, dry_run=False
    )
    assert result["status"] == "blocked"
    assert len(port.submitted) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_total_cost", float("nan")),
        ("max_daily_cost", -1),
        ("max_operation_cost", float("inf")),
    ],
)
def test_budget_values_must_be_finite(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        config(**{field: value})


@pytest.mark.asyncio
async def test_receipts_are_cumulative_not_added_again_each_poll(
    tmp_path: Path,
) -> None:
    cfg, port = config(), FixturePort(config())
    await participation_tick(cfg, port, state_dir=tmp_path, now=NOW, dry_run=False)
    result = await participation_tick(
        cfg, port, state_dir=tmp_path, now=NOW + 1, dry_run=False
    )
    assert result["cost_paid"] == 1 and result["budget_remaining"] == 8
    assert json.loads((tmp_path / "participation.json").read_text())["operations"]


@pytest.mark.asyncio
async def test_actual_overrun_is_reported_and_latches(tmp_path: Path) -> None:
    cfg, port = config(), FixturePort(config())
    receipt = port.receipt()
    receipt.update(cost=3, status="settled")
    port.receipt = lambda: receipt
    result = await participation_tick(
        cfg, port, state_dir=tmp_path, now=NOW, dry_run=False
    )
    assert result["cost_paid"] == 3 and result["risk_alert"]
    assert result["budget_remaining"] == 7
    assert "exceeded" in result["reason"]


@pytest.mark.asyncio
async def test_confirmed_points_come_only_from_observation(tmp_path: Path) -> None:
    cfg, port = config(), FixturePort(config())
    port.observation["rewards"] = [
        {
            "unit": "FLOP_TEST_ALLOCATION",
            "amount": 10,
            "status": "locked",
            "scope": "program",
            "evidence": "fixture:distribution",
            "observed_at": NOW,
        }
    ]
    result = await participation_tick(
        cfg, port, state_dir=tmp_path, now=NOW, dry_run=False
    )
    assert result["observation"]["rewards"][0]["status"] == "locked"
    assert result["completed_work"] == 0 and result["pending_operations"] == 1


def test_inference_requires_bounded_public_work_and_acceptance_criteria() -> None:
    values = config().model_dump()
    values["work"][0]["request"]["max_output_tokens"] = 1_000_000
    with pytest.raises(ValidationError):
        ParticipationConfig.model_validate(values)
    values["work"][0]["request"] = {"execute": "arbitrary code"}
    with pytest.raises(ValidationError):
        ParticipationConfig.model_validate(values)
