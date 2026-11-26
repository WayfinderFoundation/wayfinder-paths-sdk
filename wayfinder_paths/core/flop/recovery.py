"""Getting escrow back from channels a session left open.

A session normally settles or refunds before it returns. When it cannot (the miner went
quiet, the box restarted mid-session, a forced settlement is waiting out its challenge
window), the channel is remembered here and closed out later by the recurring job.
"""

from __future__ import annotations

from typing import Any

from wayfinder_paths.core.flop import state
from wayfinder_paths.core.flop.chain import FlopChain, FlopChainError
from wayfinder_paths.core.flop.signer import FlopSigner

_RECOVERY_CALL = {
    "PendingAck": "expire_force_open",
    "Open": "timeout",
    "Settling": "finalize",
}


def remember_channel(channel_id: str, reason: str) -> None:
    pending = state.read_state().get("pending_channels", {})
    pending[channel_id] = {"reason": reason}
    state.update_state(pending_channels=pending)


def pending_channels() -> dict[str, Any]:
    return state.read_state().get("pending_channels", {})


async def recover_channels(
    chain: FlopChain, signer: FlopSigner
) -> list[dict[str, Any]]:
    """Try the one recovery call each remembered channel's state allows.

    The chain refuses a call made too early (a channel still within its lifetime or
    challenge window); that channel simply stays on the list for the next run.
    """
    outcomes: list[dict[str, Any]] = []
    pending = pending_channels()
    for channel_id in list(pending):
        view = await chain.channel(bytes.fromhex(channel_id.removeprefix("0x")))
        if view.channel is None or view.channel.state == "Closed":
            pending.pop(channel_id)
            outcomes.append({"channel_id": channel_id, "outcome": "closed"})
            continue
        call = _RECOVERY_CALL.get(view.channel.state)
        if call is None:
            outcomes.append(
                {"channel_id": channel_id, "outcome": f"waiting ({view.channel.state})"}
            )
            continue
        try:
            sent = await chain.submit(
                "ComputeChannel", call, {"channel_id": channel_id}, signer
            )
        except FlopChainError as exc:
            outcomes.append(
                {"channel_id": channel_id, "outcome": "not_yet", "detail": str(exc)}
            )
            continue
        pending.pop(channel_id)
        outcomes.append(
            {
                "channel_id": channel_id,
                "outcome": call,
                "transaction": sent.extrinsic_hash,
            }
        )
    state.update_state(pending_channels=pending)
    return outcomes
