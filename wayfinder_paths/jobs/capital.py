"""Owner capital flows for a job: the ledger of deposits and withdrawals.

`state/capital_flows.jsonl` is the single source of truth for external money
moving in or out of a job's venue account. Everything that needs "capital at
time t" (forward equity curve, regime-health denominators, probation) reads
it, so a flow is a step on the curve instead of a rebase of history. The risk
peak follows flows too (``apply_flows_to_risk_peak``): a withdrawal must never
read as a drawdown.

A withdrawal larger than the venue's free margin waits in
`state/pending_withdrawal.json` (at most one). While it waits, live sizing
sees the account value minus the pending amount, and the driver settles it
once enough margin is free.

Deliberately light imports: forward views, regime health and the worker
payload all read this module.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from wayfinder_paths.jobs.compute_lock import ComputeLockBusy, job_state_lock
from wayfinder_paths.jobs.models import DEFAULT_FORWARD_TICKS, utc_now_iso
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.runner.monitor_state import atomic_write_text

CAPITAL_FLOWS_PATH = "state/capital_flows.jsonl"
PENDING_WITHDRAWAL_PATH = "state/pending_withdrawal.json"
FUNDING_MARKER_PATH = "state/funding.json"
EQUITY_RECON_PATH = "state/equity_recon.json"
RISK_STATE_PATH = "state/risk_state.json"
CAPITAL_TRANSFER_PATH = "state/capital_transfer.json"
RISK_DEFERRAL_PATH = "state/capital_risk_deferral.json"
DEFAULT_CAPITAL = 10_000.0
SUMMARY_FLOW_LIMIT = 10
LOCK_TIMEOUT_S = 60.0
# An owner transfer waits this long for a live tick's risk step to finish.
TRANSFER_LOCK_TIMEOUT_S = 180.0
# A transfer still marked in progress after this long is presumed dead (a
# deposit waits at most ~2 min for its credit): risk checks resume.
STALE_TRANSFER_S = 600.0
# An unconfirmed deposit counts once this share of it shows in venue equity.
UNCONFIRMED_DEPOSIT_VISIBLE_SHARE = 0.9

FlowKind = Literal["deposit", "withdrawal"]


@contextmanager
def capital_lock(
    store: JobStore, job_id: str, *, timeout_s: float = LOCK_TIMEOUT_S
) -> Iterator[None]:
    """Serializes ledger rewrites and pending-withdrawal transitions across the
    CLI, the MCP server and the live tick (reentrant within one thread). The
    live tick passes ``timeout_s=0`` and skips the step when busy."""
    with job_state_lock(
        store.repo_root, job_id, name="capital_flows", timeout_s=timeout_s
    ):
        yield


def signed_amount(flow: Mapping[str, Any]) -> float:
    amount = float(flow["amount"])
    return amount if flow["kind"] == "deposit" else -amount


def capital_flows(store: JobStore, job_id: str) -> list[dict[str, Any]]:
    return store.read_jsonl(job_id, CAPITAL_FLOWS_PATH)


def record_capital_flow(
    store: JobStore,
    job_id: str,
    kind: FlowKind,
    amount: float,
    *,
    capital_delta: float,
    by: str,
    destination: str | None = None,
    tx: str | None = None,
    deferred: bool = False,
    equity_before: float | None = None,
    confirmed: bool = True,
) -> dict[str, Any]:
    """Append one venue transfer. ``amount`` is what moved on the venue
    (drives the risk-peak rebase); ``capital_delta`` is how much
    ``initial_capital`` changed (drives capital_at — they differ when a first
    deposit replaces a placeholder or a withdrawal is floored at zero).
    ``equity_before`` is the venue equity read just before the transfer; an
    unconfirmed deposit (credit not yet seen) waits for it to show."""
    flow: dict[str, Any] = {
        "id": uuid.uuid4().hex[:12],
        "ts": utc_now_iso(),
        "kind": kind,
        "amount": float(amount),
        "capital_delta": float(capital_delta),
        "by": by,
        "equity_applied": False,
        "confirmed": confirmed,
        "equity_before": equity_before,
    }
    if destination:
        flow["destination"] = destination
    if tx:
        flow["tx"] = tx
    if deferred:
        flow["deferred"] = True
    with capital_lock(store, job_id):
        path = store.job_dir(job_id) / CAPITAL_FLOWS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(flow, sort_keys=True) + "\n")
    return flow


def has_capital_history(store: JobStore, job_id: str) -> bool:
    """True once the job's capital is real money rather than a paper
    placeholder: it was venue-funded through this flow, a live tick seeded the
    equity reconciler, or a flow was recorded. Deposits add to real capital
    and replace a placeholder."""
    funding = store.read_json(job_id, FUNDING_MARKER_PATH, default=None) or {}
    if funding.get("venue_funded"):
        return True
    recon = store.read_json(job_id, EQUITY_RECON_PATH, default=None) or {}
    if float(recon.get("venue_equity_start") or 0.0) > 0:
        return True
    return bool(capital_flows(store, job_id))


def funded_capital(store: JobStore, job_id: str) -> float:
    job = store.load(job_id)
    return float(job.execution_params.get("initial_capital") or 0.0)


def set_funded_capital(store: JobStore, job_id: str, value: float) -> float | None:
    """Write ``execution_params.initial_capital`` and journal it. No backend
    sync — callers outside a live tick sync afterwards."""
    job = store.load(job_id)
    previous = job.execution_params.get("initial_capital")
    job.execution_params["initial_capital"] = float(value)
    job.touch()
    store.save(job)
    store.append_journal(
        job_id,
        {"type": "operator_initial_capital_set", "from": previous, "to": value},
    )
    return previous


def _as_utc(value: str | datetime) -> datetime:
    stamp = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    )
    return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp


@dataclass(frozen=True)
class CapitalTimeline:
    """Funded capital over time: today's ``initial_capital`` with every later
    flow's capital change taken back out. Pre-ledger jobs have no flows, so
    their whole history keeps today's capital, exactly as before."""

    current: float
    steps: tuple[tuple[datetime, float], ...]

    def at(self, ts: str | datetime) -> float:
        moment = _as_utc(ts)
        return self.current - sum(delta for when, delta in self.steps if when > moment)


def capital_timeline(
    store: JobStore, job_id: str, *, default: float = DEFAULT_CAPITAL
) -> CapitalTimeline:
    raw = store.load(job_id).execution_params.get("initial_capital")
    current = float(raw) if raw is not None else default
    steps = tuple(
        (_as_utc(flow["ts"]), float(flow["capital_delta"]))
        for flow in capital_flows(store, job_id)
    )
    return CapitalTimeline(current=current, steps=steps)


def capital_at(store: JobStore, job_id: str, ts: str | datetime) -> float:
    return capital_timeline(store, job_id).at(ts)


def pending_withdrawal(store: JobStore, job_id: str) -> dict[str, Any] | None:
    return store.read_json(job_id, PENDING_WITHDRAWAL_PATH, default=None)


def set_pending_withdrawal(
    store: JobStore,
    job_id: str,
    amount: float,
    *,
    destination: str | None,
    by: str,
    withdrawable_now: float,
) -> dict[str, Any]:
    with capital_lock(store, job_id):
        if pending_withdrawal(store, job_id) is not None:
            raise ValueError(
                "a withdrawal is already pending for this job — cancel it first"
            )
        pending = {
            "amount": float(amount),
            "destination": destination,
            "requested_at": utc_now_iso(),
            "by": by,
        }
        store.write_json(job_id, PENDING_WITHDRAWAL_PATH, pending)
    store.append_journal(
        job_id,
        {
            "type": "withdrawal_pending",
            "amount": float(amount),
            "withdrawable_now": withdrawable_now,
            "by": by,
        },
    )
    return pending


def update_pending_withdrawal(
    store: JobStore, job_id: str, changes: Mapping[str, Any]
) -> dict[str, Any]:
    with capital_lock(store, job_id):
        pending = pending_withdrawal(store, job_id)
        if pending is None:
            raise ValueError("no withdrawal is pending for this job")
        updated = {**pending, **changes}
        store.write_json(job_id, PENDING_WITHDRAWAL_PATH, updated)
    return updated


def clear_pending_withdrawal(store: JobStore, job_id: str) -> dict[str, Any] | None:
    with capital_lock(store, job_id):
        pending = pending_withdrawal(store, job_id)
        (store.job_dir(job_id) / PENDING_WITHDRAWAL_PATH).unlink(missing_ok=True)
    return pending


def cancel_pending_withdrawal(
    store: JobStore, job_id: str, *, by: str
) -> dict[str, Any] | None:
    cancelled = clear_pending_withdrawal(store, job_id)
    if cancelled is not None:
        store.append_journal(
            job_id,
            {"type": "withdrawal_cancelled", "pending": cancelled, "by": by},
        )
    return cancelled


def capital_transfer_status(store: JobStore, job_id: str) -> dict[str, Any]:
    return store.read_json(job_id, CAPITAL_TRANSFER_PATH, default=None) or {}


def _in_progress(status: Mapping[str, Any]) -> bool:
    return bool(status.get("started_at")) and not status.get("finished_at")


def _age_s(status: Mapping[str, Any]) -> float:
    return (datetime.now(UTC) - _as_utc(status["started_at"])).total_seconds()


@contextmanager
def capital_transfer(
    store: JobStore,
    job_id: str,
    kind: str,
    *,
    timeout_s: float = TRANSFER_LOCK_TIMEOUT_S,
) -> Iterator[dict[str, Any]]:
    """Hold the capital lock for a whole owner transfer — the venue call and
    the flow recording. A live tick that observes venue equity while money is
    moving but before its flow exists would fold the transfer into the risk
    peak and then rescale it again (a $50 deposit on an $87 account read as a
    36% drawdown). The status file carries a sequence number so a tick can
    tell a transfer overlapped its observation even after it finished, and a
    start time so a hung transfer goes stale instead of blocking risk checks
    forever. flock releases on every exit path, crashes included."""
    with capital_lock(store, job_id, timeout_s=timeout_s):
        previous = capital_transfer_status(store, job_id)
        status = {
            "seq": int(previous.get("seq") or 0) + 1,
            "id": uuid.uuid4().hex[:12],
            "kind": kind,
            "started_at": utc_now_iso(),
        }
        store.write_json(job_id, CAPITAL_TRANSFER_PATH, status)
        try:
            yield status
        finally:
            store.write_json(
                job_id, CAPITAL_TRANSFER_PATH, {**status, "finished_at": utc_now_iso()}
            )


@contextmanager
def capital_risk_step(
    store: JobStore, job_id: str, *, observed: Mapping[str, Any]
) -> Iterator[str | None]:
    """Guard a live tick's risk step (peak rescale + drawdown check). Yields
    None when it may run, or the id of the owner transfer that defers it for
    this tick. ``observed`` is the transfer status read before the tick
    fetched venue equity: a transfer in progress then, running now, or
    finished in between (sequence moved) means the observed equity may hold
    money whose flow is not recorded yet. Never blocks."""
    with ExitStack() as stack:
        try:
            stack.enter_context(capital_lock(store, job_id, timeout_s=0))
            locked = True
        except ComputeLockBusy:
            locked = False
        current = capital_transfer_status(store, job_id)
        if locked:
            if _in_progress(current):
                # Lock free yet marked running: the transfer process died.
                _mark_stale(store, job_id, current, lock_held=True)
                current = capital_transfer_status(store, job_id)
            overlapped = current.get("seq") != observed.get("seq") or (
                _in_progress(observed) and not current.get("stale")
            )
            yield _defer(store, job_id, current) if overlapped else None
        elif _in_progress(current) and _age_s(current) > STALE_TRANSFER_S:
            _mark_stale(store, job_id, current, lock_held=False)
            yield None
        else:
            yield _defer(store, job_id, current)


def _mark_stale(
    store: JobStore, job_id: str, status: Mapping[str, Any], *, lock_held: bool
) -> None:
    deferral = store.read_json(job_id, RISK_DEFERRAL_PATH, default=None) or {}
    if lock_held:
        store.write_json(
            job_id,
            CAPITAL_TRANSFER_PATH,
            {**status, "finished_at": utc_now_iso(), "stale": True},
        )
    elif deferral.get("stale_journaled") == status.get("id"):
        return
    else:
        store.write_json(
            job_id, RISK_DEFERRAL_PATH, {**deferral, "stale_journaled": status["id"]}
        )
    store.append_journal(
        job_id,
        {
            "type": "capital_transfer_stale",
            "transfer": status.get("id"),
            "kind": status.get("kind"),
            "started_at": status.get("started_at"),
            "still_locked": not lock_held,
        },
    )


def _defer(store: JobStore, job_id: str, status: Mapping[str, Any]) -> str:
    transfer_id = str(status.get("id") or "unknown")
    deferral = store.read_json(job_id, RISK_DEFERRAL_PATH, default=None) or {}
    if deferral.get("deferred_journaled") != transfer_id:
        store.write_json(
            job_id, RISK_DEFERRAL_PATH, {**deferral, "deferred_journaled": transfer_id}
        )
        store.append_journal(
            job_id,
            {
                "type": "risk_check_deferred_capital_transfer",
                "transfer": transfer_id,
                "kind": status.get("kind"),
            },
        )
    return transfer_id


def apply_flows_to_risk_peak(
    store: JobStore,
    job_id: str,
    *,
    equity_now: float,
    observed_at: datetime,
) -> list[dict[str, Any]]:
    """Rescale the persisted venue peak for every flow not yet applied, once:
    ``peak *= equity_after / equity_before`` per flow (the unitised/NAV
    method — withdrawing half the account halves the peak, so drawdown % is
    unchanged; a deposit lifts it proportionally). The factor uses the flow's
    recorded ``equity_before`` when present, so market moves between the flow
    and this tick stay out of the peak. Only flows recorded before
    ``observed_at`` (when this tick fetched venue equity) are applied, and an
    unconfirmed deposit only once its credit shows in ``equity_now``. The live
    tick calls this inside ``capital_risk_step``; it returns [] when another
    process holds the ledger."""
    try:
        with capital_lock(store, job_id, timeout_s=0):
            events = _rebase_due_flows(
                store, job_id, equity_now=equity_now, observed_at=observed_at
            )
    except ComputeLockBusy:
        return []
    for event in events:
        store.append_journal(job_id, event)
    return events


def _rebase_due_flows(
    store: JobStore, job_id: str, *, equity_now: float, observed_at: datetime
) -> list[dict[str, Any]]:
    flows = capital_flows(store, job_id)
    due = [
        flow
        for flow in flows
        if not flow.get("equity_applied")
        and _as_utc(flow["ts"]) <= observed_at
        and _credit_visible(flow, equity_now)
    ]
    if not due:
        return []
    risk_state = store.read_json(job_id, RISK_STATE_PATH, default=None) or {}
    # Only a venue peak is rescaled: a modelled (paper) peak is discarded and
    # reseeded from venue equity on the first live risk check anyway.
    venue_peak = (
        float(risk_state["peak_equity"])
        if "peak_equity" in risk_state and risk_state.get("equity_source") == "venue"
        else None
    )
    events: list[dict[str, Any]] = []
    # equity_now already includes every due flow; walk them forward from the
    # equity before the first one.
    equity = equity_now - sum(signed_amount(flow) for flow in due)
    for flow in due:
        recorded_before = flow.get("equity_before")
        before = equity if recorded_before is None else float(recorded_before)
        after = before + signed_amount(flow)
        if venue_peak is not None:
            peak_before = venue_peak
            # An unfunded account has no drawdown to preserve; max(peak,
            # equity) in the risk check lifts the peak on its own.
            if before > 0 and after > 0:
                venue_peak = venue_peak * after / before
            events.append(
                {
                    "type": "risk_peak_rebased",
                    "flow": flow["id"],
                    "kind": flow["kind"],
                    "amount": flow["amount"],
                    "peak_before": peak_before,
                    "peak_after": venue_peak,
                }
            )
        equity += signed_amount(flow)
    if venue_peak is not None:
        risk_state["peak_equity"] = venue_peak
        risk_state["equity"] = equity_now
        risk_state["drawdown"] = (
            equity_now / venue_peak - 1.0 if venue_peak > 0 else 0.0
        )
        risk_state["updated_at"] = observed_at.isoformat()
        store.write_json(job_id, RISK_STATE_PATH, risk_state)
    applied = {flow["id"] for flow in due}
    rows = [
        {**flow, "equity_applied": True, "confirmed": True}
        if flow["id"] in applied
        else flow
        for flow in flows
    ]
    atomic_write_text(
        store.job_dir(job_id) / CAPITAL_FLOWS_PATH,
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
    )
    return events


def _credit_visible(flow: Mapping[str, Any], equity_now: float) -> bool:
    if flow.get("confirmed", True) or flow.get("equity_before") is None:
        return True
    threshold = float(flow["equity_before"]) + (
        UNCONFIRMED_DEPOSIT_VISIBLE_SHARE * float(flow["amount"])
    )
    return equity_now >= threshold


def _latest_live_account_value(store: JobStore, job_id: str) -> float | None:
    for tick in reversed(store.read_jsonl(job_id, DEFAULT_FORWARD_TICKS, limit=5)):
        if tick.get("mode") != "live":
            continue
        value = ((tick.get("snapshot") or {}).get("data") or {}).get("account_value")
        if value is not None:
            return float(value)
    return None


def capital_summary(store: JobStore, job_id: str) -> dict[str, Any]:
    """Owner capital context for sync and the wake payload."""
    flows = capital_flows(store, job_id)
    return {
        "bankroll_usd": _latest_live_account_value(store, job_id),
        "funded_capital_usd": funded_capital(store, job_id),
        "flows": flows[-SUMMARY_FLOW_LIMIT:],
        "pending_withdrawal": pending_withdrawal(store, job_id),
    }
