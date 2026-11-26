"""What the FLOP tools do: account, inference (a direct session from the user's own FLOP
account), sub-agent."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from wayfinder_paths.core.clients.FlopClient import FLOP_CLIENT
from wayfinder_paths.core.config import get_flop_config
from wayfinder_paths.core.flop import recovery, state
from wayfinder_paths.core.flop.account import flop_to_raw, raw_to_flop
from wayfinder_paths.core.flop.chain import FlopChain
from wayfinder_paths.core.flop.networks import FlopNetwork, UnknownFlopNetworkError
from wayfinder_paths.core.flop.networks import for_account as _network_for_account
from wayfinder_paths.core.flop.session import DirectSessionError, run_direct_session
from wayfinder_paths.core.flop.signer import BackendFlopSigner

# What one turn offers the miner. FLOP Testnet lists no prices, and a cooperative
# settlement pays the miner the whole escrow, so this is the price offered per turn.
DIRECT_ESCROW_FLOP = "0.05"
OWN_ACCOUNT = "own_account"


class FlopError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Inference:
    text: str
    model: str
    paid_by: str
    prompt_tokens: int
    completion_tokens: int
    cost_flop: str | None
    channel_id: str | None = None
    miner: str | None = None
    settlement: str | None = None
    transaction: str | None = None
    request_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


async def connected_account() -> dict[str, Any]:
    account = await FLOP_CLIENT.get_account()
    if not account.get("connected"):
        raise FlopError(
            "flop_not_connected",
            "No FLOP account is connected. Run flop_connect first.",
        )
    return account


async def default_model() -> str:
    """The strongest model the connected account's network serves."""
    return network_for(await connected_account()).default_model


def signer_for(account: dict[str, Any]) -> BackendFlopSigner:
    return BackendFlopSigner(
        bytes.fromhex(account["account_id"].removeprefix("0x")), FLOP_CLIENT
    )


def network_for(account: dict[str, Any]) -> FlopNetwork:
    try:
        return _network_for_account(account)
    except UnknownFlopNetworkError as exc:
        raise FlopError("flop_unknown_network", str(exc)) from exc


def chain_for(account: dict[str, Any]) -> FlopChain:
    # The configured RPC overrides the profile's; FlopChain still refuses a node whose
    # genesis is not the account's network.
    return FlopChain(
        rpc_url=get_flop_config()["rpc_url"] or network_for(account).rpc_url,
        expected_genesis_hash=account["genesis_hash"],
    )


def spend_ceiling_raw(max_cost_flop: str | None) -> int:
    config = get_flop_config()
    per_call = flop_to_raw(config["per_call_flop"])
    ceiling = min(flop_to_raw(max_cost_flop), per_call) if max_cost_flop else per_call
    remaining_today = flop_to_raw(config["daily_flop"]) - state.spent_today()
    if remaining_today <= 0:
        raise FlopError(
            "flop_daily_cap_reached",
            f"This box's daily FLOP spend cap ({config['daily_flop']} FLOP) is used up.",
        )
    return min(ceiling, remaining_today)


def _pick_miner(miner: str | None) -> tuple[str, str] | None:
    miners: dict[str, str] = get_flop_config()["miners"]
    if miner and miner not in miners:
        raise FlopError(
            "flop_unknown_miner",
            "That miner has no endpoint in system.flop.miners.",
        )
    target = (miner, miners[miner]) if miner else next(iter(miners.items()), None)
    # The session client also accepts loopback HTTP, which is for people running a
    # miner on the same host. A box never is one, and its loopback ports are its own
    # services, so only a public HTTPS endpoint is accepted here.
    if target is not None and not target[1].startswith("https://"):
        raise FlopError(
            "flop_miner_endpoint_not_https",
            "Miner endpoints in system.flop.miners must be https:// URLs.",
        )
    return target


async def infer(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    max_tokens: int = 256,
    max_cost_flop: str | None = None,
    miner: str | None = None,
) -> Inference:
    """One chat turn bought from a miner over a direct session, paid from the user's own
    FLOP account. FLOP Testnet has no hosted gateway, so a miner must be configured."""
    account = await connected_account()
    model = model or network_for(account).default_model
    signer = signer_for(account)
    ceiling_raw = spend_ceiling_raw(max_cost_flop)
    target = _pick_miner(miner)
    if target is None:
        raise FlopError(
            "flop_no_miner_endpoint",
            "FLOP Testnet has no inference gateway: inference is a direct session with "
            "a miner listed in system.flop.miners, and none is configured.",
        )
    result = await _direct(
        account, signer, target, model, messages, max_tokens, ceiling_raw
    )
    state.record_activity(
        {k: v for k, v in result.as_dict().items() if k != "text"}
        | {"kind": "inference"}
    )
    return result


def direct_escrow_raw(ceiling_raw: int) -> int:
    """What one turn escrows: the fixed offer, or less if the caps leave less."""
    return min(flop_to_raw(DIRECT_ESCROW_FLOP), ceiling_raw)


async def _direct(
    account: dict[str, Any],
    signer: BackendFlopSigner,
    target: tuple[str, str],
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    ceiling_raw: int,
) -> Inference:
    miner, endpoint = target
    escrow_raw = direct_escrow_raw(ceiling_raw)
    chain = chain_for(account)
    try:
        session = await run_direct_session(
            chain=chain,
            signer=signer,
            miner=miner,
            model=model,
            endpoint=endpoint,
            messages=messages,
            max_flop_raw=escrow_raw,
            max_tokens=max_tokens,
        )
    except DirectSessionError as exc:
        channel_hex = "0x" + exc.channel_id.hex()
        if exc.recovery != "refunded":
            recovery.remember_channel(channel_hex, exc.recovery or "session_failed")
        state.record_activity(
            {
                "kind": "inference_failed",
                "model": model,
                "miner": miner,
                "channel_id": channel_hex,
                "recovery": exc.recovery,
            }
        )
        raise
    finally:
        chain.close()
    if session.settlement == "posted":
        # A forced settlement releases the unspent escrow only after its challenge window.
        recovery.remember_channel(session.channel_id, "awaiting_finalize")
    state.add_spend(session.payable)
    receipt = session.receipts[0]
    return Inference(
        text=session.reply,
        model=model,
        paid_by=OWN_ACCOUNT,
        prompt_tokens=receipt.prompt_tokens or 0,
        completion_tokens=receipt.completion_tokens or 0,
        cost_flop=raw_to_flop(session.payable),
        channel_id=session.channel_id,
        miner=miner,
        settlement=session.settlement,
        transaction=session.settle_tx,
    )


WORKER_SYSTEM = (
    "You are a focused worker. Do exactly the task you are given. Be concise and "
    "concrete. If the task cannot be done from the information provided, say what is "
    "missing in one sentence. Output only the result."
)
REVIEW_SYSTEM = (
    "You are checking a draft answer against its task. If the draft is correct and "
    "complete, repeat it unchanged. Otherwise output a corrected final answer. Output "
    "only the final answer."
)


async def run_subagent(
    task: str,
    *,
    context: str | None = None,
    model: str | None = None,
    max_steps: int = 2,
    max_tokens: int = 384,
    max_cost_flop: str | None = None,
) -> dict[str, Any]:
    """A small worker that runs entirely on a FLOP model: draft, then self-review.

    FLOP's current models have short context and no tool calling, so the worker gets
    one self-contained task and each step is its own inference.
    """
    if not 1 <= max_steps <= 4:
        raise FlopError("invalid_argument", "max_steps must be between 1 and 4")
    model = model or await default_model()
    brief = task if not context else f"{task}\n\nContext:\n{context}"
    steps: list[Inference] = []
    draft = await infer(
        [
            {"role": "system", "content": WORKER_SYSTEM},
            {"role": "user", "content": brief},
        ],
        model=model,
        max_tokens=max_tokens,
        max_cost_flop=max_cost_flop,
    )
    steps.append(draft)
    answer = draft.text
    for _ in range(max_steps - 1):
        review = await infer(
            [
                {"role": "system", "content": REVIEW_SYSTEM},
                {
                    "role": "user",
                    "content": f"Task:\n{brief}\n\nDraft answer:\n{answer}",
                },
            ],
            model=model,
            max_tokens=max_tokens,
            max_cost_flop=max_cost_flop,
        )
        steps.append(review)
        if review.text.strip() == answer.strip():
            break
        answer = review.text
    return {
        "answer": answer,
        "model": model,
        "steps": [{k: v for k, v in s.as_dict().items() if k != "text"} for s in steps],
        "paid_by": steps[-1].paid_by,
    }
