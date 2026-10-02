"""Objective-aware activities in the existing freestyle job lifecycle.

The author chooses work, not credentials, adapters, or spending ceilings.
Capabilities and limits come from the revisioned job configuration. This is
not a sandbox for arbitrary Python; executable extensions require review.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from wayfinder_paths.jobs.participation import (
    ParticipationConfig,
    ParticipationPort,
    Record,
    WorkItem,
    participation_tick,
)


class Objective(Record):
    metric: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,99}$")
    unit: str = Field(min_length=1, max_length=64)
    direction: Literal["maximize", "minimize"]
    # A description is not a measurement or an implied dollar valuation.
    description: str = Field(default="", max_length=500)


class ActivityBinding(Record):
    capability: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,99}$")
    limits: ParticipationConfig
    options: dict[str, Any] = Field(default_factory=dict)
    extension: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,63}$")


class ObjectiveStrategy(Record):
    # Default retains the existing trading protections for hybrid strategies.
    trading_enabled: bool = True
    primary: Objective
    secondary: list[Objective] = Field(default_factory=list, max_length=8)
    activities: dict[str, ActivityBinding] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def check_names(self) -> ObjectiveStrategy:
        import re

        if any(not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", k) for k in self.activities):
            raise ValueError("activity names must be lowercase identifiers")
        metrics = [self.primary.metric, *(o.metric for o in self.secondary)]
        if len(set(metrics)) != len(metrics):
            raise ValueError("objective metrics must be unique")
        return self


def objective_strategy(params: Mapping[str, Any]) -> ObjectiveStrategy | None:
    raw = params.get("objective_strategy")
    return ObjectiveStrategy.model_validate(raw) if raw is not None else None


def builtin_adapter(binding: ActivityBinding) -> ParticipationPort:
    """Only compiled-in, reviewed integrations; never import a config string."""
    from wayfinder_paths.adapters.reward_participation_adapter.adapter import (
        RewardParticipationAdapter,
    )

    expected = f"{binding.limits.protocol}.participation"
    if binding.capability != expected or binding.limits.protocol not in {
        "flop",
        "risex",
        "perptools",
        "imd",
    }:
        raise ValueError(f"unregistered activity capability: {binding.capability}")
    if set(binding.options) - {"seat_id"}:
        raise ValueError(
            "unsupported activity options; credentials must not be in job.yaml"
        )
    return RewardParticipationAdapter(
        config=binding.limits.model_dump(mode="json"),
        # Account-scoped credential is runtime-only; never serialized in receipts.
        risex_token=os.environ.get("RISEX_JWT"),
        imd_seat_id=binding.options.get("seat_id"),
    )


async def run_activity(
    binding: ActivityBinding,
    *,
    state_dir: Path,
    now: float,
    mode: str,
    dry_run: bool,
    halted: bool,
    work: list[WorkItem] | None = None,
    adapter: ParticipationPort | None = None,
    extension_pin: dict[str, Any] | None = None,
    job_root: Path | None = None,
) -> dict[str, Any]:
    """Paper observes real receipts but never submits or changes the live ledger.

    Built-in validation makes no protocol calls. It does not invoke extension
    lifecycle methods either, but reviewed Python factories are not sandboxed.
    The result states that the external lifecycle was not verified.
    An injected adapter is a test seam, not a job-configurable escape hatch.
    """
    config = binding.limits.model_copy(deep=True)
    if work is not None:
        config.work = work
    config = ParticipationConfig.model_validate(config.model_dump(mode="json"))
    if halted:
        config.enabled = False
    paper = dry_run or mode != "live"
    if binding.extension and adapter is None:
        if extension_pin is None or job_root is None:
            raise ValueError("activity extension is not pinned on this job")
        # Dry-run validation must not call external adapters. Load only their
        # reviewed factory to verify the declared lifecycle; do not fake success.
        from wayfinder_paths.jobs.activity_extensions import load_activity_extension

        adapter = load_activity_extension(
            job_root,
            extension_pin,
            capability=binding.capability,
            config=config.model_dump(mode="json"),
            options=binding.options,
        )
        if dry_run:
            await adapter.close()
            return {
                "status": "unverified",
                "dry_run": True,
                "external_verified": False,
                "capability": binding.capability,
                "reason": "extension execution requires separate verification",
            }
        try:
            # Attaching code is not certification of live execution. Initial
            # Path extensions are observation-only, regardless of their flags.
            if not paper and config.enabled:
                raise ValueError(
                    "Path activity execution is not certified for live use"
                )
            return {
                **await participation_tick(
                    config, adapter, state_dir=state_dir, now=now, dry_run=True
                ),
                "capability": binding.capability,
                "mode": mode,
            }
        finally:
            await adapter.close()
    if dry_run and adapter is None:
        check = builtin_adapter(binding)
        await check.close()
        return {
            "status": "unverified",
            "dry_run": True,
            "reason": "validation does not submit external work or simulate rewards",
            "capability": binding.capability,
            "cost_unit": config.cost_unit,
            "requested_work": len(config.work),
            "external_verified": False,
        }
    owned = adapter is None
    adapter = adapter or builtin_adapter(binding)
    try:
        result = await participation_tick(
            config, adapter, state_dir=state_dir, now=now, dry_run=paper
        )
        return {**result, "capability": binding.capability, "mode": mode}
    finally:
        if owned:
            await adapter.close()


def activity_live_blockers(params: Mapping[str, Any]) -> list[str]:
    """Readiness cannot be enabled by setting `enabled` in an author bundle."""
    spec = objective_strategy(params)
    if spec is None:
        return []
    blockers = []
    for name, binding in spec.activities.items():
        # All initial network integrations are observation-only until their
        # execution, reconciliation and protective monitoring are certified.
        if binding.limits.enabled:
            blockers.append(
                f"{name}: {binding.capability} has no verified live execution interface"
            )
    return blockers
