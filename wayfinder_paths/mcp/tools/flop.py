from __future__ import annotations

import time
from typing import Any

import httpx

from wayfinder_paths.core.clients.FlopClient import FLOP_CLIENT
from wayfinder_paths.core.config import get_flop_config
from wayfinder_paths.core.flop import faucet, jobs, recovery, service, state
from wayfinder_paths.core.flop.account import raw_to_flop
from wayfinder_paths.core.flop.chain import FlopChainError
from wayfinder_paths.core.flop.session import DirectSessionError
from wayfinder_paths.core.utils.wallets import create_remote_wallet, load_remote_wallets
from wayfinder_paths.mcp.utils import catch_errors, err, ok, throw_if_empty_str

FLOP_WALLET_LABEL = "flop"
UNSET = "_"


def _optional(value: str) -> str | None:
    return None if value in ("", UNSET) else value


def _backend_error(exc: httpx.HTTPStatusError) -> dict[str, Any]:
    try:
        body = exc.response.json()
    except ValueError:
        body = {}
    return err(
        str(body.get("code") or f"http_{exc.response.status_code}"),
        str(body.get("error") or f"HTTP {exc.response.status_code}"),
    )


def _failure(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, httpx.HTTPStatusError):
        return _backend_error(exc)
    if isinstance(exc, service.FlopError):
        return err(exc.code, str(exc))
    if isinstance(exc, DirectSessionError):
        return err(
            "flop_session_failed",
            str(exc),
            {
                "channel_id": "0x" + exc.channel_id.hex(),
                "recovery": exc.recovery,
                "note": "any escrow still locked is recovered by the flop-faucet job",
            },
        )
    if isinstance(exc, FlopChainError):
        return err("flop_chain_error", str(exc))
    raise exc


async def _collect(account: dict[str, Any]) -> faucet.FaucetResult:
    try:
        return await faucet.collect_if_due(
            service.signer_for(account), service.network_for(account)
        )
    except (httpx.HTTPError, ValueError, faucet.FaucetIdentityError) as exc:
        return faucet.FaucetResult(
            granted=False, reason="faucet_unavailable", message=str(exc)[:200]
        )


async def _strategy_wallet_address(label: str) -> str | None:
    for wallet in await load_remote_wallets():
        if (
            wallet["label"] == label
            and wallet["chain_type"] == "ethereum"
            and wallet["wallet_type"] == "strategy"
        ):
            return wallet["address"]
    return None


@catch_errors
async def flop_status() -> dict:
    """Show this account's FLOP connection: address, balance, wallet session, caps, faucet.

    FLOP is a separate network for AI inference; this account is on FLOP Testnet, whose
    FLOP has no value. `connected: false` means no FLOP account exists yet; call
    flop_connect.
    """
    try:
        account = await FLOP_CLIENT.get_account()
    except httpx.HTTPStatusError as exc:
        return _backend_error(exc)
    if not account.get("connected"):
        return ok({"connected": False, "network": account.get("network")})
    address = account["flop_address"]
    chain = service.chain_for(account)
    try:
        balance = await chain.balance(address)
    except (FlopChainError, OSError) as exc:
        balance = None
        balance_error = str(exc)
    finally:
        chain.close()
    config = get_flop_config()
    return ok(
        {
            "connected": True,
            "network": account["network"],
            "flop_address": address,
            "wallet_address": account["wallet_address"],
            "session_expires_at": account.get("session_expires_at"),
            "balance_flop": raw_to_flop(balance.free) if balance else None,
            **({"balance_error": balance_error} if balance is None else {}),
            "box_caps": {
                "per_call_flop": config["per_call_flop"],
                "daily_flop": config["daily_flop"],
                "spent_today_flop": raw_to_flop(state.spent_today()),
            },
            "account_limits": account.get("limits"),
            "direct_miners_configured": len(config["miners"]),
            "faucet_next_eligible_at": faucet.next_eligible_at(address) or None,
            "faucet_waiting_for_spend": faucet.waiting_for_spend(address),
            # Scheduling is repeated here so a box whose runner was down when the
            # account was connected still gets its faucet job.
            "faucet_job": jobs.ensure_faucet_job(),
            "pending_channels": len(recovery.pending_channels()),
        }
    )


@catch_errors
async def flop_connect(wallet_label: str = FLOP_WALLET_LABEL) -> dict:
    """Connect a FLOP account for this user (one per Wayfinder account).

    Uses a dedicated strategy wallet named `wallet_label` (created if it does not exist)
    whose key becomes the FLOP account; that wallet can then sign only for FLOP. Also
    schedules the recurring faucet job and asks the faucet for a first grant.
    """
    label = throw_if_empty_str("wallet_label is required", wallet_label)
    try:
        account = await FLOP_CLIENT.get_account()
        if not account.get("connected"):
            address = await _strategy_wallet_address(label)
            if address is None:
                created = await create_remote_wallet(label, "strategy")
                address = created["evm"]["wallet_address"]
            account = await FLOP_CLIENT.connect(address)
    except httpx.HTTPStatusError as exc:
        return _backend_error(exc)
    job = jobs.ensure_faucet_job()
    grant = await _collect(account)
    return ok(
        {
            "flop_address": account["flop_address"],
            "wallet_address": account["wallet_address"],
            "network": account["network"],
            "faucet_job": job,
            "faucet": grant.as_dict(),
        }
    )


@catch_errors
async def flop_models() -> dict:
    """List the models FLOP Testnet has verified, and which one inference uses by default.

    Testnet lists no prices and no per-model limits. Which models a given miner runs is
    known only once a session opens with it. Current FLOP models have short context and
    no tool calling.
    """
    try:
        account = await service.connected_account()
        default = service.network_for(account).default_model
        chain = service.chain_for(account)
        try:
            names = await chain.verified_models()
        finally:
            chain.close()
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)
    return ok([{"id": name, "default": name == default} for name in names])


@catch_errors
async def flop_estimate(max_cost_flop: str = UNSET) -> dict:
    """The most one flop_infer call can cost right now.

    FLOP Testnet lists no prices: each call offers the miner a fixed escrow, and a
    cooperative settlement pays all of it. This reports that escrow after the per-call
    cap, the daily cap already spent, and `max_cost_flop` if given.
    """
    try:
        ceiling = service.spend_ceiling_raw(_optional(max_cost_flop))
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)
    return ok({"max_cost_flop": raw_to_flop(service.direct_escrow_raw(ceiling))})


@catch_errors
async def flop_infer(
    prompt: str,
    model: str = UNSET,
    max_tokens: int = 256,
    max_cost_flop: str = UNSET,
    system: str = UNSET,
    miner: str = UNSET,
) -> dict:
    """Run one prompt on a FLOP model and pay for it in FLOP.

    Opens a compute channel from the user's own FLOP account with a miner listed in
    system.flop.miners (the first one unless `miner` names another), verifies the miner's
    signed receipt and settles. FLOP Testnet has no hosted gateway, so without a
    configured miner this returns flop_no_miner_endpoint. Without `model` it uses the
    strongest model Testnet serves (see flop_models).

    Spend is capped per call and per day (see flop_status). Use this for real work the
    user or task needs; do not loop it to generate activity.
    """
    text = throw_if_empty_str("prompt is required", prompt)
    messages = [{"role": "user", "content": text}]
    if _optional(system):
        messages.insert(0, {"role": "system", "content": system})
    try:
        result = await service.infer(
            messages,
            model=_optional(model),
            max_tokens=int(max_tokens),
            max_cost_flop=_optional(max_cost_flop),
            miner=_optional(miner),
        )
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)
    return ok(result.as_dict())


@catch_errors
async def flop_faucet() -> dict:
    """Ask the FLOP faucet for this account's daily grant, if one is due.

    The faucet gives one grant per address per day. On testnet the claim is signed by
    the account, and the next grant needs the last one spent on inference
    (reason "agent_spend_missing"). A refusal is reported with when to try again; it is
    not retried. A recurring job also does this automatically.
    """
    try:
        account = await service.connected_account()
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)
    result = await _collect(account)
    return ok({"address": account["flop_address"], **result.as_dict()})


@catch_errors
async def flop_activity(limit: int = 20) -> dict:
    """Recent FLOP activity: the account's compute channels and this box's inference log."""
    try:
        remote = await FLOP_CLIENT.get_activity(limit=int(limit))
    except httpx.HTTPStatusError as exc:
        return _backend_error(exc)
    local = state.read_state().get("activity", [])[-int(limit) :]
    return ok(
        {
            "balance": remote.get("balance"),
            "channels": remote.get("channels", []),
            "degraded": bool(remote.get("degraded")),
            "inferences": list(reversed(local)),
            "as_of": int(time.time()),
        }
    )


@catch_errors
async def flop_subagent(
    task: str,
    context: str = UNSET,
    model: str = UNSET,
    max_steps: int = 2,
    max_tokens: int = 384,
    max_cost_flop: str = UNSET,
) -> dict:
    """Hand one self-contained task to a worker that runs entirely on a FLOP model.

    The worker drafts an answer and then reviews it (max_steps inferences in total, each
    paid in FLOP and capped like flop_infer). It has no tools and a short context, so
    give it everything it needs in `task` and `context`: good for summarising,
    classifying, extracting, rewriting and short reasoning; not for anything that needs
    live data or long documents.
    """
    brief = throw_if_empty_str("task is required", task)
    try:
        result = await service.run_subagent(
            brief,
            context=_optional(context),
            model=_optional(model),
            max_steps=int(max_steps),
            max_tokens=int(max_tokens),
            max_cost_flop=_optional(max_cost_flop),
        )
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)
    return ok(result)
