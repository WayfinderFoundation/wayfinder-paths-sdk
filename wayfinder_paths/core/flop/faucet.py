"""The FLOP faucets. One grant per address per day; a refusal is never retried in a loop.

Devnet funds an address on request (`POST /fund`). Testnet takes signed agent claims
(`POST /v1/testnet/agent-claims`): the account signs `flop-faucet-agent/v1|<genesis>|
<address>|<nonce>`, and a claim repeated with the same nonce and signature returns the same
transfer and never pays twice. That is how an unclear outcome is reconciled, so a claim's
nonce and signature are kept until the faucet gives it a final answer, and a grant counts
(and starts the 24h cooldown) only once its transfer is finalized: an included transfer can
still be dropped.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from wayfinder_paths.core.flop import state
from wayfinder_paths.core.flop.account import (
    account_id_from_public_key,
    recover_compressed_public_key,
)
from wayfinder_paths.core.flop.networks import TESTNET, FlopNetwork
from wayfinder_paths.core.flop.signer import FlopSigner
from wayfinder_paths.core.flop.wire import blake2_256

GRANT_COOLDOWN_SECONDS = 24 * 3600
UNKNOWN_HOLD_SECONDS = 3600
# A transfer the faucet reports as included but not yet finalized is checked again (by
# repeating the same claim) after this long.
TRANSFER_PENDING_HOLD_SECONDS = 600
# A request the faucet called malformed is not sent again for this long.
FIX_REQUEST_HOLD_SECONDS = 6 * 3600
AGENT_CLAIM_PATH = "/v1/testnet/agent-claims"
AGENT_CLAIM_VERSION = "flop-faucet-agent/v1"
# The faucet also counts grants per caller IP. Shells share outbound addresses, so that
# limit can refuse a Shell for other people's requests.
SHARED_ADDRESS_HINT = (
    "The faucet also limits grants per IP address, and Shells share outbound "
    "addresses, so this refusal may be for someone else's request."
)
DEVNET_PANEL_HINT = (
    "Use Get test FLOP in the FLOP panel, which asks from the user's own browser."
)
SPEND_NEEDED_MESSAGE = (
    "The testnet faucet gives the next grant only after the last one has been spent "
    "on inference. The job asks again after this account's next FLOP spend."
)
TRANSFER_PENDING_MESSAGE = (
    "The faucet sent the grant, but its transfer is not final yet. The same claim is "
    "repeated until the faucet reports it finalized; it cannot pay twice."
)
TIMEOUT_SECONDS = 60
MAX_RESPONSE_BYTES = 64 * 1024
NONCE_PATTERN = re.compile(r"^(0|[1-9][0-9]{0,18})$")


class FaucetIdentityError(Exception):
    """The faucet does not serve the network the account is on."""


@dataclass(frozen=True)
class FaucetResult:
    granted: bool
    amount_flop: float = 0
    tx_hash: str | None = None
    transfer_status: str | None = None
    reason: str | None = None
    message: str | None = None
    action: str | None = None
    retry_at: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def agent_claim_message(genesis_hash: str, address: str, nonce: str) -> str:
    if not NONCE_PATTERN.match(nonce):
        raise ValueError("nonce must be a canonical decimal of 1-19 digits")
    return "|".join((AGENT_CLAIM_VERSION, genesis_hash.lower(), address, nonce))


def _json(response: httpx.Response) -> dict[str, Any]:
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise ValueError("faucet response too large")
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError("faucet response is not an object")
    return body


async def _get(network: FlopNetwork, path: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        response = await client.get(f"{network.faucet_url}{path}")
        response.raise_for_status()
        return _json(response)


async def limits(network: FlopNetwork) -> dict[str, Any]:
    return await _get(network, "/limits")


async def check_identity(network: FlopNetwork) -> None:
    """The faucet must say it serves this network's genesis before we sign or ask."""
    chain = await _get(network, "/chain")
    if (
        chain.get("network") != network.name
        or str(chain.get("genesisHash", "")).lower() != network.genesis_hash
    ):
        raise FaucetIdentityError(
            f"{network.faucet_url} serves {chain.get('network')!r} "
            f"(genesis {chain.get('genesisHash')}), not FLOP {network.name}"
        )


def _unknown(current: int) -> FaucetResult:
    # A timeout or an unreadable body: the grant may or may not have been made. Hold
    # off, and let the balance (or, on testnet, the same claim repeated) show what happened.
    return FaucetResult(
        granted=False,
        reason="unknown",
        message="the faucet gave no readable answer; check the balance later",
        action="retry_later",
        retry_at=current + UNKNOWN_HOLD_SECONDS,
    )


def _classify(
    response: httpx.Response, body: dict[str, Any], current: int, *, hint: str
) -> FaucetResult:
    if response.status_code == 200 and body.get("success"):
        status = body.get("status")
        return FaucetResult(
            granted=True,
            amount_flop=float(body.get("amount", 0)),
            tx_hash=body.get("txHash"),
            transfer_status=status if status in ("included", "finalized") else None,
        )
    retry_after = body.get("retryAfterSeconds") or response.headers.get("Retry-After")
    reason = str(body.get("reason") or f"http_{response.status_code}")
    message = str(body.get("error") or "the faucet refused the request")
    action = body.get("action")
    if _per_ip_refusal(response.status_code, reason, message):
        message = f"{message}. {hint}"
        action = "use_flop_panel_faucet" if hint == DEVNET_PANEL_HINT else action
    return FaucetResult(
        granted=False,
        reason=reason,
        message=message,
        action=action,
        retry_at=current + int(float(retry_after)) if retry_after else None,
    )


async def fund(
    address: str, network: FlopNetwork, *, now: float | None = None
) -> FaucetResult:
    """Devnet: ask the faucet to fund `address`."""
    current = int(now if now is not None else time.time())
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{network.faucet_url}/fund", json={"address": address}
            )
        body = _json(response)
    except (httpx.HTTPError, ValueError):
        return _unknown(current)
    return _classify(response, body, current, hint=DEVNET_PANEL_HINT)


async def _claim_signature(
    signer: FlopSigner, network: FlopNetwork, nonce: str
) -> tuple[str, str]:
    message = agent_claim_message(network.genesis_hash, signer.address, nonce)
    signature = await signer.sign_faucet_claim(message, nonce)
    # The backend builds the text it signs; make sure it is this text, for this account.
    recovered = recover_compressed_public_key(blake2_256(message.encode()), signature)
    if account_id_from_public_key(recovered) != signer.account_id:
        raise ValueError("the faucet claim signature is not the account's")
    return message, "0x" + signature.hex()


async def claim(
    signer: FlopSigner,
    network: FlopNetwork = TESTNET,
    *,
    pending: dict[str, str] | None = None,
    now: float | None = None,
) -> tuple[FaucetResult, dict[str, str] | None]:
    """Testnet: send (or repeat) a signed agent claim for the signer's address.

    Returns the result and the claim to keep for a repeat, or None once the faucet has
    answered it for good.
    """
    current = int(now if now is not None else time.time())
    if pending is None:
        nonce = str(current * 1000)
        _, signature = await _claim_signature(signer, network, nonce)
        pending = {"nonce": nonce, "signature": signature}
        _save_pending(signer.address, pending)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{network.faucet_url}{AGENT_CLAIM_PATH}",
                json={
                    "address": signer.address,
                    "nonce": pending["nonce"],
                    "signature": pending["signature"],
                },
            )
        body = _json(response)
    except (httpx.HTTPError, ValueError):
        return _unknown(current), pending
    result = _classify(response, body, current, hint=SHARED_ADDRESS_HINT)
    if result.granted and result.transfer_status == "finalized":
        return result, None
    if result.granted:
        # Included (or no status at all) is not final: the transfer can still be dropped.
        # Keep the claim and its transaction; the same claim, repeated, reports the same
        # transfer, so only a finalized answer counts as the grant.
        if result.tx_hash:
            pending = {**pending, "tx_hash": result.tx_hash}
        return (
            FaucetResult(
                granted=False,
                amount_flop=result.amount_flop,
                tx_hash=result.tx_hash,
                transfer_status=result.transfer_status or "unconfirmed",
                reason="transfer_not_final",
                message=TRANSFER_PENDING_MESSAGE,
                action="retry_later",
                retry_at=current + TRANSFER_PENDING_HOLD_SECONDS,
            ),
            pending,
        )
    if response.status_code >= 500 and result.action != "retry_later":
        return _unknown(current), pending
    if result.action == "retry_later":
        # "Wait, then repeat the same request": the same nonce and signature.
        return result, pending
    if result.reason == "agent_spend_missing":
        return (
            FaucetResult(
                granted=False,
                reason=result.reason,
                message=SPEND_NEEDED_MESSAGE,
                action="spend_first",
            ),
            None,
        )
    return result, None


def _save_pending(address: str, pending: dict[str, str] | None) -> None:
    faucet_state = state.read_state().get("faucet", {})
    record = dict(faucet_state.get(address, {}))
    record["pending_claim"] = pending
    faucet_state[address] = record
    state.update_state(faucet=faucet_state)


def _per_ip_refusal(status: int, reason: str, message: str) -> bool:
    """A refusal for the caller's IP rather than the FLOP address being funded."""
    words = set(re.split(r"[^a-z0-9]+", reason.lower()))
    return bool(words & {"ip", "client", "prefix"}) or (
        status == 429 and re.search(r"\bip\b", message.lower()) is not None
    )


def _record(address: str) -> dict[str, Any]:
    return dict(state.read_state().get("faucet", {}).get(address, {}))


def next_eligible_at(address: str) -> int:
    record = _record(address)
    return max(
        int(record.get("last_grant_at", 0)) + GRANT_COOLDOWN_SECONDS
        if record.get("last_grant_at")
        else 0,
        int(record.get("retry_at") or 0),
    )


def waiting_for_spend(address: str) -> bool:
    """The faucet asked for a spend, and this box has recorded none since."""
    since = _record(address).get("spend_required_since")
    return bool(since) and state.last_spend_at() <= int(since)


async def collect_if_due(
    signer: FlopSigner, network: FlopNetwork, *, now: float | None = None
) -> FaucetResult:
    """Ask for the daily grant unless we already know the faucet will say no."""
    current = int(now if now is not None else time.time())
    address = signer.address
    record = _record(address)
    pending = record.get("pending_claim")
    eligible_at = next_eligible_at(address)
    if current < eligible_at:
        return FaucetResult(
            granted=False,
            reason="not_due",
            message="the last grant or refusal is too recent",
            retry_at=eligible_at,
        )
    if not pending and waiting_for_spend(address):
        return FaucetResult(
            granted=False,
            reason="agent_spend_missing",
            message=SPEND_NEEDED_MESSAGE,
            action="spend_first",
        )
    await check_identity(network)
    faucet_limits = (await limits(network)).get("faucet", {})
    if faucet_limits.get("status") != "open" and not pending:
        return FaucetResult(
            granted=False,
            reason="faucet_closed",
            message=f"faucet status is {faucet_limits.get('status')!r}",
        )
    if network.name == TESTNET.name:
        claim_path = (faucet_limits.get("agent") or {}).get("claimPath")
        if claim_path != AGENT_CLAIM_PATH:
            return FaucetResult(
                granted=False,
                reason="faucet_closed",
                message=f"the faucet offers no agent claims (claim path {claim_path!r})",
            )
        result, keep = await claim(signer, network, pending=pending, now=current)
    else:
        result, keep = await fund(address, network, now=current), None
    record = _record(address)
    record["pending_claim"] = keep
    if result.granted:
        record.update(
            last_grant_at=current,
            retry_at=None,
            last_amount=result.amount_flop,
            last_tx_hash=result.tx_hash,
            spend_required_since=None,
            last_refusal=None,
        )
    elif result.reason == "agent_spend_missing":
        record.update(spend_required_since=current, retry_at=None)
        record["last_refusal"] = result.reason
    else:
        hold = (
            FIX_REQUEST_HOLD_SECONDS
            if result.action == "fix_request"
            else UNKNOWN_HOLD_SECONDS
        )
        # Without a wait from the faucet, hold off rather than ask again at once.
        record.update(retry_at=result.retry_at or current + hold)
        record["last_refusal"] = result.reason
    faucet_state = state.read_state().get("faucet", {})
    faucet_state[address] = record
    state.update_state(faucet=faucet_state)
    return result
