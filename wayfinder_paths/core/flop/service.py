"""What the FLOP tools do: account, inference (own-account channel or gateway), sub-agent."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from wayfinder_paths.core.clients.FlopClient import FLOP_CLIENT
from wayfinder_paths.core.config import get_flop_config
from wayfinder_paths.core.flop import gateway, recovery, state
from wayfinder_paths.core.flop.account import flop_to_raw, raw_to_flop
from wayfinder_paths.core.flop.chain import FlopChain
from wayfinder_paths.core.flop.networks import FlopNetwork, UnknownFlopNetworkError
from wayfinder_paths.core.flop.networks import for_account as _network_for_account
from wayfinder_paths.core.flop.session import DirectSessionError, run_direct_session
from wayfinder_paths.core.flop.signer import BackendFlopSigner

DEFAULT_MODEL = "qwen/qwen2.5-0.5b-instruct"
# Used when FLOP's catalogue has no price for the model being asked for.
FALLBACK_DIRECT_ESCROW_FLOP = "0.05"
OWN_ACCOUNT = "own_account"
GATEWAY_OPERATOR = "gateway_operator"


class FlopError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Inference:
    text: str
    model: str
    mode: str
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


def estimate_cost_flop(
    model: dict[str, Any], prompt_chars: int, max_tokens: int
) -> Decimal:
    """A ceiling for one call from the model's listed per-token price.

    Three characters per token overstates the prompt for English, which is the safe side.
    """
    pricing = model.get("pricing") or {}
    prompt_tokens = math.ceil(prompt_chars / 3)
    return prompt_tokens * Decimal(
        str(pricing.get("prompt", 0))
    ) + max_tokens * Decimal(str(pricing.get("completion", 0)))


def _spend_ceiling_raw(max_cost_flop: str | None) -> int:
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
    model: str = DEFAULT_MODEL,
    max_tokens: int = 256,
    max_cost_flop: str | None = None,
    mode: str = "auto",
    miner: str | None = None,
) -> Inference:
    if mode not in ("auto", "direct", "gateway"):
        raise FlopError("invalid_argument", "mode must be auto, direct or gateway")
    account = await connected_account()
    signer = signer_for(account)
    ceiling_raw = _spend_ceiling_raw(max_cost_flop)
    target = None if mode == "gateway" else _pick_miner(miner)
    if mode == "direct" and target is None:
        raise FlopError(
            "flop_no_miner_endpoint",
            "A direct session needs a miner endpoint in system.flop.miners.",
        )
    if target is None and network_for(account).gateway_url is None:
        raise FlopError(
            "flop_no_gateway",
            f"FLOP {network_for(account).name} has no inference gateway yet. Inference "
            "there is a direct session with a miner listed in system.flop.miners.",
        )
    if target is not None:
        result = await _direct(
            account, signer, target, model, messages, max_tokens, ceiling_raw
        )
    else:
        result = await _through_gateway(
            signer, model, messages, max_tokens, ceiling_raw
        )
    state.record_activity(
        {k: v for k, v in result.as_dict().items() if k != "text"}
        | {"kind": "inference"}
    )
    return result


async def _direct_escrow_raw(
    model: str, messages: list[dict[str, str]], max_tokens: int, ceiling_raw: int
) -> int:
    """What to escrow for one turn.

    A cooperative settlement pays the miner the whole escrow, so the escrow is the price
    offered, not a ceiling: size it from the model's listed price.
    """
    try:
        listed = await gateway.models()
    except (gateway.GatewayError, OSError):
        listed = []
    match = next((m for m in listed if m["id"].lower() == model.lower()), None)
    prompt_chars = sum(len(m.get("content", "")) for m in messages)
    estimate = (
        estimate_cost_flop(match, prompt_chars, max_tokens) if match else Decimal(0)
    )
    offered = flop_to_raw(
        format(estimate.quantize(Decimal(1).scaleb(-18)), "f")
        if estimate > 0
        else FALLBACK_DIRECT_ESCROW_FLOP
    )
    return min(offered, ceiling_raw)


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
    escrow_raw = await _direct_escrow_raw(model, messages, max_tokens, ceiling_raw)
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
                "mode": "direct",
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
        mode="direct",
        paid_by=OWN_ACCOUNT,
        prompt_tokens=receipt.prompt_tokens or 0,
        completion_tokens=receipt.completion_tokens or 0,
        cost_flop=raw_to_flop(session.payable),
        channel_id=session.channel_id,
        miner=miner,
        settlement=session.settlement,
        transaction=session.settle_tx,
    )


async def _through_gateway(
    signer: BackendFlopSigner,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    ceiling_raw: int,
) -> Inference:
    key = await gateway.ensure_key(signer)
    if key["model"].lower() != model.lower():
        raise FlopError(
            "flop_gateway_model_scope",
            f"This account's gateway key only serves {key['model']}.",
        )
    reply = await gateway.chat(
        key["key"],
        model,
        messages,
        max_tokens=max_tokens,
        max_cost_flop=float(raw_to_flop(ceiling_raw)),
    )
    receipt = reply.receipt or {}
    return Inference(
        text=reply.text,
        model=model,
        mode="gateway",
        paid_by=GATEWAY_OPERATOR,
        prompt_tokens=reply.prompt_tokens,
        completion_tokens=reply.completion_tokens,
        cost_flop=str(reply.cost_flop) if reply.cost_flop is not None else None,
        channel_id=receipt.get("channelId"),
        miner=receipt.get("miner"),
        settlement=receipt.get("settlementStatus"),
        request_id=reply.request_id,
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
    model: str = DEFAULT_MODEL,
    max_steps: int = 2,
    max_tokens: int = 384,
    max_cost_flop: str | None = None,
    mode: str = "auto",
) -> dict[str, Any]:
    """A small worker that runs entirely on a FLOP model: draft, then self-review.

    FLOP's current models have short context and no tool calling, so the worker gets
    one self-contained task and each step is its own inference.
    """
    if not 1 <= max_steps <= 4:
        raise FlopError("invalid_argument", "max_steps must be between 1 and 4")
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
        mode=mode,
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
            mode=mode,
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
