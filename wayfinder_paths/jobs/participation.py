"""Bounded, receipt-driven activities for strategy jobs, not a scheduler.

The strategy supplies a reviewed capability and owner-approved limits.
The existing job runner calls this once per tick. No point-to-dollar valuation,
trading evidence requirement, retry loop, or model-generated action lives here.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from wayfinder_paths.runner.monitor_state import atomic_write_json


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Reward(Record):
    unit: str = Field(min_length=1)
    amount: FiniteFloat = Field(ge=0)
    status: Literal["pending", "confirmed", "locked", "claimable"]
    evidence: str = Field(min_length=1)
    observed_at: FiniteFloat = Field(ge=0)
    scope: Literal["lifetime", "program", "operation"]


class InferenceRequest(Record):
    """Owner-reviewed, useful public work only; no local code or secret inputs."""

    purpose: str = Field(min_length=1, max_length=300)
    public_input: str = Field(min_length=1, max_length=32000)
    model_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_output_tokens: int = Field(gt=0, le=4096)
    max_latency_seconds: int = Field(gt=0, le=120)
    acceptance_criteria: str = Field(min_length=1, max_length=1000)


class Observation(Record):
    protocol: str
    program: str
    rule_revision: str
    observed_at: FiniteFloat = Field(ge=0)
    readiness: Literal["ready", "blocked", "observe_only"]
    eligible: bool | None = None
    reason: str = ""
    rewards: list[Reward] = Field(default_factory=list)
    # Compact, explicitly normalized metrics; never raw responses or credentials.
    metrics: dict[str, FiniteFloat | str | None] = Field(default_factory=dict)
    metric_units: dict[str, str] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)


class WorkItem(Record):
    id: str = Field(min_length=1, max_length=100)
    kind: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    max_cost: FiniteFloat = Field(gt=0)
    # Protocol adapter must validate this schema and any spend/exposure limits.
    request: dict[str, Any]

    @model_validator(mode="after")
    def validate_inference(self) -> WorkItem:
        if self.kind == "inference":
            InferenceRequest.model_validate(self.request)
        return self


class MetricConstraint(Record):
    unit: str = Field(min_length=1)
    minimum: FiniteFloat | None = None
    maximum: FiniteFloat | None = None

    @model_validator(mode="after")
    def check_bounds(self) -> MetricConstraint:
        if self.minimum is None and self.maximum is None:
            raise ValueError("a constraint needs a minimum or maximum")
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError("constraint minimum exceeds maximum")
        return self


class ParticipationConfig(Record):
    protocol: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    program: str = Field(min_length=1)
    rule_revision: str = Field(min_length=1)
    account: str = ""
    enabled: bool = False
    cost_unit: str = Field(min_length=1)
    max_total_cost: FiniteFloat = Field(ge=0)
    max_daily_cost: FiniteFloat = Field(ge=0)
    max_operation_cost: FiniteFloat = Field(ge=0)
    max_daily_operations: int = Field(default=1, ge=1, le=100)
    max_pending_seconds: int = Field(default=600, ge=1, le=86400)
    observation_max_age_seconds: int = Field(default=300, ge=1, le=3600)
    work: list[WorkItem] = Field(default_factory=list, max_length=100)
    # Adapter-normalized measurements, e.g. net_delta/USD or cpu_seconds/seconds.
    constraints: dict[str, MetricConstraint] = Field(
        default_factory=dict, max_length=16
    )


class Receipt(Record):
    status: Literal["pending", "settled", "failed"]
    request_id: str = Field(min_length=1)
    # Cumulative charged amount, not an incremental fee. Terminal must be final.
    cost: FiniteFloat = Field(ge=0)
    cost_unit: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    work_accepted: bool | None = None
    qualifying_activity: bool | None = None


class Operation(Record):
    id: str
    work_id: str
    request_hash: str
    started_at: FiniteFloat = Field(ge=0)
    max_cost: FiniteFloat = Field(gt=0)
    receipt: Receipt | None = None
    settled_at: FiniteFloat | None = None


class ParticipationState(Record):
    identity: str
    operations: dict[str, Operation] = Field(default_factory=dict)
    # A breach/ambiguous submission never clears itself on the next tick.
    blocked: str | None = None


class ParticipationSnapshot(Record):
    status: str
    reason: str | None = None
    dry_run: bool = False
    risk_alert: bool = False
    observation: Observation | None = None
    cost_unit: str | None = None
    cost_paid: FiniteFloat | None = None
    budget_remaining: FiniteFloat | None = None
    operations: int = 0
    completed_work: int = 0
    qualifying_activity: int = 0
    pending_operations: int = 0


class ParticipationPort(Protocol):
    supports_submit: bool

    async def close(self) -> None: ...

    async def observe(self) -> tuple[bool, dict[str, Any] | str]: ...

    async def protect(self, *, dry_run: bool) -> tuple[bool, str]:
        """Reconcile risk and maintain exits even while rewards are paused.

        Trading adapters own durable, fill-driven hedge/exit recovery here. They
        must not treat missing/ambiguous account state as flat or open risk.
        """
        ...

    async def submit(
        self, item: WorkItem, *, operation_id: str
    ) -> tuple[bool, dict[str, Any] | str]: ...

    async def reconcile(
        self, *, operation_id: str, request_id: str | None
    ) -> tuple[bool, dict[str, Any] | str]: ...


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def _reserved(op: Operation) -> float:
    if op.receipt and op.receipt.status != "pending":
        return op.receipt.cost
    return max(op.max_cost, op.receipt.cost if op.receipt else 0)


async def participation_tick(
    config: ParticipationConfig,
    adapter: ParticipationPort,
    *,
    state_dir: Path,
    now: float,
    dry_run: bool,
) -> dict[str, Any]:
    """One observation/reconciliation and at most one new bounded submission.

    Persist BEFORE submitting. A crash after acceptance but before receipt save
    resumes by operation ID; even an adapter error never authorizes resubmission.
    Dry runs neither sign/submit nor touch the live operation ledger.
    """
    if len({item.id for item in config.work}) != len(config.work):
        raise ValueError("work item IDs must be unique")
    state_dir.mkdir(parents=True, exist_ok=True)
    # Non-reentrant cross-process AND cross-coroutine lock. Holding it across
    # adapter awaits prevents overlapping runner/manual ticks from duplicating.
    with (state_dir / "participation.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "busy", "reason": "participation tick already running"}
        return await _tick(
            config, adapter, state_dir=state_dir, now=now, dry_run=dry_run
        )


async def _tick(
    config: ParticipationConfig,
    adapter: ParticipationPort,
    *,
    state_dir: Path,
    now: float,
    dry_run: bool,
) -> dict[str, Any]:
    # Risk maintenance precedes every permission, readiness and budget check.
    started = time.monotonic()
    safe, safety_reason = await adapter.protect(dry_run=dry_run)
    identity = _hash(
        [config.protocol, config.program, config.account, config.cost_unit]
    )
    state_path = state_dir / "participation.json"
    try:
        state = (
            ParticipationState.model_validate_json(state_path.read_text())
            if state_path.exists()
            else ParticipationState(identity=identity)
        )
        if state.identity != identity:
            raise ValueError(
                "account/program/unit changed; reconcile the old job first"
            )
    except (OSError, ValueError):
        # Never overwrite unreadable state or restore an empty spend allowance.
        return {
            "status": "blocked",
            "dry_run": dry_run,
            "risk_alert": True,
            "reason": "participation state unreadable or identity changed",
        }

    def save() -> None:
        if not dry_run:
            atomic_write_json(state_path, state.model_dump(mode="json"))

    if not safe:
        state.blocked = "risk protection failed: " + safety_reason
        save()
    # Reconcile outstanding work even if disabled, ineligible or budget exhausted.
    pending = [
        op
        for op in state.operations.values()
        if not op.receipt or op.receipt.status == "pending"
    ]
    for op in pending:
        ok, data = await adapter.reconcile(
            operation_id=op.id,
            request_id=op.receipt.request_id if op.receipt else None,
        )
        if ok:
            receipt = Receipt.model_validate(data)
            if receipt.cost_unit != config.cost_unit or (
                op.receipt
                and (
                    receipt.cost < op.receipt.cost
                    or receipt.request_id != op.receipt.request_id
                )
            ):
                state.blocked = (
                    "receipt cost unit changed or cumulative charge decreased"
                )
            else:
                op.receipt = receipt
                if receipt.status != "pending":
                    op.settled_at = now
                if receipt.cost > op.max_cost:
                    state.blocked = "protocol exceeded the reserved operation cost"
        if now - op.started_at > config.max_pending_seconds and (
            not op.receipt or op.receipt.status == "pending"
        ):
            state.blocked = (
                "operation unresolved past deadline; reconcile before rearming"
            )
        save()

    ok, raw = await adapter.observe()
    observation = Observation.model_validate(raw) if ok else None
    # Account for I/O latency when checking freshness and the daily budget.
    now += time.monotonic() - started
    committed = sum(_reserved(op) for op in state.operations.values())
    today = [
        op
        for op in state.operations.values()
        if int(op.started_at // 86400) == int(now // 86400)
    ]
    # Older in-flight work still reserves against today's cap: it can settle today.
    daily = sum(
        _reserved(op)
        for op in state.operations.values()
        if op in today
        or not op.receipt
        or op.receipt.status == "pending"
        or (
            op.settled_at is not None
            and int(op.settled_at // 86400) == int(now // 86400)
        )
    )
    reason = state.blocked
    if not reason and observation is None:
        reason = "observation unavailable; no new activity"
    if observation and not reason:
        if (observation.protocol, observation.program, observation.rule_revision) != (
            config.protocol,
            config.program,
            config.rule_revision,
        ):
            reason = "program rules changed; owner review required"
        elif (
            not -5
            <= now - observation.observed_at
            <= config.observation_max_age_seconds
        ):
            reason = "stale observation"
        elif observation.readiness != "ready" or observation.eligible is not True:
            reason = observation.reason or "readiness/eligibility not confirmed"
    if observation and not reason:
        for metric, bound in config.constraints.items():
            value = observation.metrics.get(metric)
            if (
                not isinstance(value, (float, int))
                or observation.metric_units.get(metric) != bound.unit
            ):
                reason = f"required constraint measurement unavailable: {metric} ({bound.unit})"
                break
            if (bound.minimum is not None and value < bound.minimum) or (
                bound.maximum is not None and value > bound.maximum
            ):
                state.blocked = f"constraint breached: {metric}={value:g} {bound.unit}"
                reason = state.blocked
                save()
                break
    if not reason and not config.enabled:
        reason = "activity disabled; observing only"
    if not reason and not adapter.supports_submit:
        reason = "adapter has no verified submission interface"
    if not reason and any(
        not op.receipt or op.receipt.status == "pending"
        for op in state.operations.values()
    ):
        reason = "awaiting reconciliation"

    status = "blocked" if reason else "idle"
    if not reason:
        for item in config.work:
            request_hash = _hash(item.model_dump(mode="json"))
            existing = next(
                (op for op in state.operations.values() if op.work_id == item.id), None
            )
            if existing:
                if existing.request_hash != request_hash:
                    reason = "work item changed after submission; use a new reviewed ID"
                    status = "blocked"
                    break
                continue
            if (
                item.max_cost > config.max_operation_cost
                or committed + item.max_cost > config.max_total_cost
                or daily + item.max_cost > config.max_daily_cost
                or len(today) >= config.max_daily_operations
            ):
                reason, status = "activity budget exhausted", "blocked"
                break
            if dry_run:
                status = "dry_run"
                break
            operation_id = _hash([identity, item.id, request_hash])
            op = Operation(
                id=operation_id,
                work_id=item.id,
                request_hash=request_hash,
                started_at=now,
                max_cost=item.max_cost,
            )
            state.operations[operation_id] = op
            save()
            # Do not interpret ok=False/timeout as proof the protocol rejected it.
            ok, data = await adapter.submit(item, operation_id=operation_id)
            if ok:
                receipt = Receipt.model_validate(data)
                if receipt.cost_unit != config.cost_unit:
                    state.blocked = "submission receipt exceeded cost contract"
                else:
                    op.receipt = receipt
                    if receipt.status != "pending":
                        op.settled_at = now
                    if receipt.cost > item.max_cost:
                        state.blocked = "submission receipt exceeded cost contract"
            save()
            status = "submitted" if ok and not state.blocked else "reconcile_required"
            break

    return {
        "status": status,
        "reason": state.blocked or reason,
        "dry_run": dry_run,
        "risk_alert": bool(state.blocked),
        "observation": observation.model_dump(mode="json") if observation else None,
        "cost_unit": config.cost_unit,
        "cost_paid": sum(
            op.receipt.cost for op in state.operations.values() if op.receipt
        ),
        "budget_remaining": max(
            0.0,
            config.max_total_cost
            - sum(_reserved(op) for op in state.operations.values()),
        ),
        "operations": len(state.operations),
        "completed_work": sum(
            bool(op.receipt and op.receipt.work_accepted)
            for op in state.operations.values()
        ),
        "qualifying_activity": sum(
            bool(op.receipt and op.receipt.qualifying_activity)
            for op in state.operations.values()
        ),
        "pending_operations": sum(
            not op.receipt or op.receipt.status == "pending"
            for op in state.operations.values()
        ),
        # Rewards come ONLY from the observed protocol ledger, never from work count.
    }
