"""The FLOP devnet faucet. One grant per address per day; a refusal is never retried in a loop."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from wayfinder_paths.core.flop import state

FAUCET_URL = "https://faucet.flop.network"
GRANT_COOLDOWN_SECONDS = 24 * 3600
UNKNOWN_HOLD_SECONDS = 3600
# The faucet also counts grants per caller IP. Shells share outbound addresses, so that
# limit can refuse a Shell for other people's requests; the user's browser is not shared.
SHARED_ADDRESS_HINT = (
    "Shells share outbound addresses, so the faucet's per-IP limit can refuse them. "
    "Use Get test FLOP in the FLOP panel, which asks from the user's own browser."
)
TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 64 * 1024


@dataclass(frozen=True)
class FaucetResult:
    granted: bool
    amount_flop: float = 0
    tx_hash: str | None = None
    reason: str | None = None
    message: str | None = None
    action: str | None = None
    retry_at: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def _json(response: httpx.Response) -> dict[str, Any]:
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise ValueError("faucet response too large")
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError("faucet response is not an object")
    return body


async def limits() -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        response = await client.get(f"{FAUCET_URL}/limits")
        response.raise_for_status()
        return _json(response)


async def fund(address: str, *, now: float | None = None) -> FaucetResult:
    current = int(now if now is not None else time.time())
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{FAUCET_URL}/fund", json={"address": address}
            )
        body = _json(response)
    except (httpx.HTTPError, ValueError):
        # A timeout or an unreadable body: the grant may or may not have been made.
        # The faucet cannot tie a request to its transaction, so asking again at once
        # could not tell either. Hold off, and let the balance show what happened.
        return FaucetResult(
            granted=False,
            reason="unknown",
            message="the faucet gave no readable answer; check the balance later",
            action="retry_later",
            retry_at=current + UNKNOWN_HOLD_SECONDS,
        )
    if response.status_code == 200 and body.get("success"):
        return FaucetResult(
            granted=True,
            amount_flop=float(body.get("amount", 0)),
            tx_hash=body.get("txHash"),
        )
    retry_after = body.get("retryAfterSeconds") or response.headers.get("Retry-After")
    reason = str(body.get("reason") or f"http_{response.status_code}")
    message = str(body.get("error") or "the faucet refused the request")
    action = body.get("action")
    if _per_ip_refusal(response.status_code, reason, message):
        message = f"{message}. {SHARED_ADDRESS_HINT}"
        action = "use_flop_panel_faucet"
    return FaucetResult(
        granted=False,
        reason=reason,
        message=message,
        action=action,
        retry_at=current + int(float(retry_after)) if retry_after else None,
    )


def _per_ip_refusal(status: int, reason: str, message: str) -> bool:
    """A refusal for the caller's IP rather than the FLOP address being funded."""
    words = set(re.split(r"[^a-z0-9]+", reason.lower()))
    return bool(words & {"ip", "client", "prefix"}) or (
        status == 429 and re.search(r"\bip\b", message.lower()) is not None
    )


def next_eligible_at(address: str) -> int:
    record = state.read_state().get("faucet", {}).get(address, {})
    return max(
        int(record.get("last_grant_at", 0)) + GRANT_COOLDOWN_SECONDS
        if record.get("last_grant_at")
        else 0,
        int(record.get("retry_at") or 0),
    )


async def collect_if_due(address: str, *, now: float | None = None) -> FaucetResult:
    """Ask for the daily grant unless we already know the faucet will say no."""
    current = int(now if now is not None else time.time())
    eligible_at = next_eligible_at(address)
    if current < eligible_at:
        return FaucetResult(
            granted=False,
            reason="not_due",
            message="the last grant or refusal is too recent",
            retry_at=eligible_at,
        )
    faucet_limits = (await limits()).get("faucet", {})
    if faucet_limits.get("status") != "open":
        return FaucetResult(
            granted=False,
            reason="faucet_closed",
            message=f"faucet status is {faucet_limits.get('status')!r}",
        )
    result = await fund(address, now=current)
    faucet_state = state.read_state().get("faucet", {})
    record = dict(faucet_state.get(address, {}))
    if result.granted:
        record.update(
            last_grant_at=current, retry_at=None, last_amount=result.amount_flop
        )
    else:
        # Without a wait from the faucet, hold off an hour rather than ask again at once.
        record.update(
            retry_at=result.retry_at or current + 3600, last_refusal=result.reason
        )
    faucet_state[address] = record
    state.update_state(faucet=faucet_state)
    return result
