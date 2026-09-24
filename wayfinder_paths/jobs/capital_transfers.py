"""Venue transfers behind a job's capital flows (Hyperliquid USDC).

The owner paths (CLI / backend relay / owner-approved chat) and the live
tick's pending-withdrawal settlement share these functions, so there is one
transfer path and one test seam: the `hyperliquid_tools` module attributes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import wayfinder_paths.mcp.tools.hyperliquid as hyperliquid_tools
from wayfinder_paths.core.constants.hyperliquid import WITHDRAW_FEE_USD
from wayfinder_paths.jobs.capital import (
    EQUITY_RECON_PATH,
    capital_lock,
    capital_transfer,
    clear_pending_withdrawal,
    funded_capital,
    pending_withdrawal,
    record_capital_flow,
    set_funded_capital,
    update_pending_withdrawal,
)
from wayfinder_paths.jobs.compute_lock import ComputeLockBusy
from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.store import JobStore


@dataclass(frozen=True)
class VenueBalance:
    equity_usd: float
    withdrawable_usd: float


async def venue_equity_or_none(wallet_label: str) -> float | None:
    """Venue equity before a deposit; None for an account the venue does not
    know yet (a first deposit creates it), which the rebase handles by
    inferring the pre-flow equity."""
    envelope = await hyperliquid_tools.hyperliquid_get_state(label=wallet_label)
    if not envelope["ok"]:
        return None
    return _balance_from_summary(envelope["result"]["summary"]).equity_usd


async def venue_balance(wallet_label: str) -> VenueBalance:
    """Venue equity and what can leave it right now: free margin (open
    positions hold the rest), less the Bridge2 fee as a buffer against the
    margin moving between this read and the withdrawal."""
    envelope = await hyperliquid_tools.hyperliquid_get_state(label=wallet_label)
    if not envelope["ok"]:
        raise ValueError(f"venue state unavailable: {envelope['error']}")
    return _balance_from_summary(envelope["result"]["summary"])


def _balance_from_summary(summary: dict[str, Any]) -> VenueBalance:
    if "unified_usdc_margin_available" in summary:
        equity = float(summary["unified_usdc_equity"])
        free = float(summary["unified_usdc_margin_available"])
    else:
        # Split accounts are unified before withdrawing, which merges spot
        # USDC into the withdrawable balance.
        spot = float(summary["spot_usdc_total"])
        equity = float(summary["perp_account_value"]) + spot
        free = float(summary["perp_withdrawable"]) + spot
    return VenueBalance(
        equity_usd=equity, withdrawable_usd=max(free - WITHDRAW_FEE_USD, 0.0)
    )


def shift_equity_recon_baseline(store: JobStore, job_id: str, delta: float) -> None:
    """Fold an owner deposit/withdrawal into the drift baseline. The equity
    reconciler treats venue-vs-expected drift as its signal; without this,
    every flow reads as permanent drift the agent has to re-explain each
    wake. Missing seed = pre-first-tick, nothing to shift."""
    recon = store.read_json(job_id, EQUITY_RECON_PATH, default=None)
    if not recon or "venue_equity_start" not in recon:
        return
    recon["venue_equity_start"] = float(recon["venue_equity_start"]) + delta
    store.write_json(job_id, EQUITY_RECON_PATH, recon)


async def deposit_to_venue(wallet_label: str, amount: float) -> dict[str, Any]:
    """Bridge USDC into Hyperliquid and wait for the credit. An `unconfirmed`
    credit still counts (the deposit is en route); only a failed send
    raises."""
    envelope = await hyperliquid_tools.hyperliquid_deposit_usdc(
        wallet_label=wallet_label, amount_usdc=amount
    )
    if not envelope["ok"]:
        raise ValueError(f"venue deposit failed: {envelope}")
    outcome = envelope["result"]
    if outcome["status"] == "failed":
        raise ValueError(f"venue deposit failed: {outcome}")
    return outcome


def deposit_tx_hash(outcome: dict[str, Any]) -> str | None:
    for effect in outcome.get("effects") or []:
        tx = (effect.get("result") or {}).get("txn_hash")
        if tx:
            return str(tx)
    return None


async def execute_withdrawal(
    store: JobStore,
    job_id: str,
    amount: float,
    *,
    wallet_label: str,
    destination: str | None,
    by: str,
    equity_before: float,
    deferred: bool = False,
) -> dict[str, Any]:
    """Submit the venue withdrawal, then record the flow, shrink
    ``initial_capital`` by the gross amount (floored at zero — a full
    withdrawal honestly reads as unfunded) and shift the drift baseline.
    Callers hold ``capital_transfer`` around this."""
    envelope = await hyperliquid_tools.submit_usdc_withdrawal(
        wallet_label=wallet_label, amount_usdc=amount, destination=destination
    )
    if not envelope["ok"]:
        raise ValueError(f"venue withdraw failed: {envelope}")
    outcome = envelope["result"]
    if outcome["status"] == "failed":
        raise ValueError(f"venue withdraw failed: {outcome}")
    current = funded_capital(store, job_id)
    capital = max(current - float(amount), 0.0)
    flow = record_capital_flow(
        store,
        job_id,
        "withdrawal",
        amount,
        capital_delta=capital - current,
        by=by,
        destination=destination,
        deferred=deferred,
        equity_before=equity_before,
    )
    set_funded_capital(store, job_id, capital)
    shift_equity_recon_baseline(store, job_id, -float(amount))
    store.append_journal(
        job_id,
        {"type": "withdrawal_executed", "flow": flow, "status": outcome["status"]},
    )
    return {
        "withdraw_status": outcome["status"],
        "initial_capital": capital,
        "flow": flow,
    }


async def settle_pending_withdrawal(
    store: JobStore, job_id: str, *, wallet_label: str | None
) -> dict[str, Any] | None:
    """Live-tick step: run the queued withdrawal once free margin covers it.
    Never halts and never raises — a failed transfer stays pending and is
    journaled; a busy ledger (an owner transfer in flight) skips this tick.
    An attempt that died mid-transfer (tick killed) is never retried blind:
    its outcome is unknown, so it waits for the owner to check the venue and
    cancel."""
    if pending_withdrawal(store, job_id) is None:
        return None
    try:
        with capital_lock(store, job_id, timeout_s=0):
            return await _settle_locked(store, job_id, wallet_label=wallet_label)
    except ComputeLockBusy:
        return {"kind": "withdrawal_busy"}


async def _settle_locked(
    store: JobStore, job_id: str, *, wallet_label: str | None
) -> dict[str, Any] | None:
    pending = pending_withdrawal(store, job_id)
    if pending is None:
        return None
    amount = float(pending["amount"])
    if pending.get("attempt_started_at"):
        if pending.get("attempt_status") != "unknown":
            update_pending_withdrawal(store, job_id, {"attempt_status": "unknown"})
            store.append_journal(
                job_id,
                {
                    "type": "withdrawal_failed",
                    "amount": amount,
                    "error": (
                        "a previous attempt was interrupted mid-transfer; its "
                        "outcome is unknown — check the venue, then cancel"
                    ),
                },
            )
        return {"kind": "withdrawal_outcome_unknown", "amount": amount}
    try:
        if not wallet_label:
            raise ValueError("job has no bound wallet (wallet_label)")
        balance = await venue_balance(wallet_label)
        if balance.withdrawable_usd < amount:
            return {
                "kind": "withdrawal_waiting",
                "amount": amount,
                "withdrawable_now": balance.withdrawable_usd,
            }
        update_pending_withdrawal(store, job_id, {"attempt_started_at": utc_now_iso()})
        try:
            with capital_transfer(store, job_id, "withdrawal", timeout_s=0):
                executed = await execute_withdrawal(
                    store,
                    job_id,
                    amount,
                    wallet_label=wallet_label,
                    destination=pending.get("destination"),
                    by=str(pending.get("by") or "owner"),
                    equity_before=balance.equity_usd,
                    deferred=True,
                )
        except Exception:
            update_pending_withdrawal(store, job_id, {"attempt_started_at": None})
            raise
        clear_pending_withdrawal(store, job_id)
    except Exception as exc:  # noqa: BLE001 — a transfer failure never fails a tick
        store.append_journal(
            job_id,
            {"type": "withdrawal_failed", "amount": amount, "error": str(exc)[:300]},
        )
        return {"kind": "withdrawal_failed", "amount": amount, "error": str(exc)}
    return {"kind": "withdrawal_executed", **executed}
