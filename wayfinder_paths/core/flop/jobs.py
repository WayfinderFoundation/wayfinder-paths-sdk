"""The recurring job for this box's FLOP account: faucet grant, escrow recovery and, on
testnet, the daily spend that keeps the grant coming."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx

from wayfinder_paths.core.clients.FlopClient import FLOP_CLIENT
from wayfinder_paths.core.clients.NotifyClient import NOTIFY_CLIENT
from wayfinder_paths.core.config import get_flop_config
from wayfinder_paths.core.flop import faucet, recovery, service, state
from wayfinder_paths.core.flop.account import RAW_PER_FLOP
from wayfinder_paths.core.flop.chain import FlopChain, FlopChainError
from wayfinder_paths.core.flop.miner import endpoint_answers
from wayfinder_paths.core.flop.networks import TESTNET, FlopNetwork
from wayfinder_paths.mcp.state.runs import runs_root
from wayfinder_paths.runner.client import RunnerControlClient
from wayfinder_paths.runner.constants import JOB_TYPE_SCRIPT
from wayfinder_paths.runner.paths import find_repo_root, get_runner_paths

FAUCET_JOB_NAME = "flop-faucet"
FAUCET_SCRIPT_NAME = "flop_faucet.py"
# The faucet allows one grant a day. Checking every six hours costs nothing when no
# grant is due (the check is local) and picks the grant up soon after it becomes due.
FAUCET_CHECK_INTERVAL_SECONDS = 6 * 3600
SESSION_WARNING_SECONDS = 24 * 3600
LOW_BALANCE_RAW = RAW_PER_FLOP
LOW_BALANCE_NOTICE_SECONDS = 3 * 24 * 3600
SPEND_INTERVAL_SECONDS = 24 * 3600
# Keep enough for the transactions a session needs (open, settle) and a recovery.
SPEND_FLOOR_RAW = RAW_PER_FLOP // 2
DAILY_SPEND_MAX_TOKENS = 192
DAILY_SPEND_PROMPT = (
    "In three short bullet points, give one practical tip each for writing clear "
    "commit messages, naming functions, and structuring a README."
)

_FAUCET_SCRIPT = '''"""Collects the daily FLOP faucet grant for this box's FLOP account, closes out any
compute channel a session left open so its escrow comes back, and on FLOP testnet spends
part of the grant on one inference a day once miners are live.

Written and scheduled by the Wayfinder SDK (job "flop-faucet"). Delete the job with
`wayfinder runner delete flop-faucet` to stop it.
"""

from wayfinder_paths.core.flop.jobs import run_faucet_job

run_faucet_job()
'''


async def collect_faucet_grant() -> dict[str, Any]:
    account = await FLOP_CLIENT.get_account()
    if not account.get("connected"):
        return {"granted": False, "reason": "flop_not_connected"}
    network = service.network_for(account)
    signer = service.signer_for(account)
    try:
        result = await faucet.collect_if_due(signer, network)
    except (httpx.HTTPError, ValueError, faucet.FaucetIdentityError) as exc:
        result = faucet.FaucetResult(
            granted=False, reason="faucet_unavailable", message=str(exc)[:200]
        )
    outcome: dict[str, Any] = {
        "address": account["flop_address"],
        "network": network.name,
        **result.as_dict(),
    }
    chain = service.chain_for(account)
    try:
        if recovery.pending_channels():
            outcome["recovered_channels"] = await recovery.recover_channels(
                chain, signer
            )
        balance_raw = (await chain.balance(account["flop_address"])).free
        supply = await chain.inference_supply() if network is TESTNET else None
        if supply is not None:
            outcome["testnet_miners"], outcome["testnet_models"] = supply
            outcome["spend"] = await spend_testnet_grant(
                balance_raw, supply, chain=chain, network=network
            )
    finally:
        chain.close()
    outcome["notices"] = await send_due_notices(account, balance_raw, supply=supply)
    return outcome


def inference_open(supply: tuple[int, int] | None) -> bool:
    """Testnet sells inference only once it has both staked miners and registered models."""
    return supply is not None and supply[0] > 0 and supply[1] > 0


async def _spend_target(
    chain: FlopChain, network: FlopNetwork
) -> tuple[tuple[str, str] | None, dict[str, str]]:
    """The first configured miner that can take today's spend, checked without escrowing.

    A miner qualifies when it is an active miner on this network, the spend model is a
    verified registry entry here, and its session endpoint answers. Returns the miner
    (address, endpoint) or None, and why each miner that was passed over was skipped.
    """
    skipped: dict[str, str] = {}
    try:
        await chain.resolve_model_identity(network.daily_spend_model)
    except FlopChainError:
        return None, {"*": "spend_model_not_registered"}
    for miner, endpoint in get_flop_config()["miners"].items():
        if not endpoint.startswith("https://"):
            skipped[miner] = "endpoint_not_https"
        elif not await chain.miner_is_active(miner):
            skipped[miner] = "miner_not_active"
        elif not await endpoint_answers(endpoint):
            skipped[miner] = "miner_unreachable"
        else:
            return (miner, endpoint), skipped
    return None, skipped


async def spend_testnet_grant(
    balance_raw: int,
    supply: tuple[int, int],
    *,
    chain: FlopChain,
    network: FlopNetwork,
    now: float | None = None,
) -> dict[str, Any]:
    """Spend part of the testnet grant on one direct session, at most once a day.

    The testnet faucet gives the next grant only after the last one has been spent on
    inference, so once testnet sells inference the job buys one small turn a day from a
    configured miner that it has checked can serve it. The amount is the box's per-call cap
    at most.
    """
    current = int(now if now is not None else time.time())
    if not inference_open(supply):
        return {"spent": False, "reason": "testnet_inference_not_open"}
    if not get_flop_config()["miners"]:
        return {"spent": False, "reason": "no_miner_endpoint"}
    if balance_raw < SPEND_FLOOR_RAW:
        return {"spent": False, "reason": "balance_below_floor"}
    if current - state.last_spend_at() < SPEND_INTERVAL_SECONDS:
        return {"spent": False, "reason": "spent_recently"}
    try:
        target, skipped = await _spend_target(chain, network)
    except Exception as exc:  # noqa: BLE001 - reported, checked again next run
        return {"spent": False, "reason": "miner_check_failed", "error": str(exc)[:200]}
    if target is None:
        return {"spent": False, "reason": "no_ready_miner", "skipped": skipped}
    try:
        result = await service.infer(
            [{"role": "user", "content": DAILY_SPEND_PROMPT}],
            model=network.daily_spend_model,
            mode="direct",
            miner=target[0],
            max_tokens=DAILY_SPEND_MAX_TOKENS,
        )
    except Exception as exc:  # noqa: BLE001 - reported, retried next run
        return {"spent": False, "reason": type(exc).__name__, "error": str(exc)[:200]}
    return {
        "spent": True,
        "cost_flop": result.cost_flop,
        "channel_id": result.channel_id,
        "miner": result.miner,
    }


async def send_due_notices(
    account: dict[str, Any],
    balance_raw: int,
    *,
    supply: tuple[int, int] | None = None,
    now: float | None = None,
) -> list[str]:
    """Tell the user, once, about the things only they can act on."""
    current = int(now if now is not None else time.time())
    sent = dict(state.read_state().get("notices", {}))
    due: list[tuple[str, int, str, str]] = []
    if inference_open(supply) and not sent.get("testnet_inference"):
        due.append(
            (
                "testnet_inference",
                current,
                "FLOP testnet now sells inference",
                "Miners are live on FLOP testnet, so your agent can now pay for "
                "inference there. Add a testnet miner endpoint under system.flop.miners "
                "so the daily FLOP job can spend its faucet grant.",
            )
        )
    expires_at = account.get("session_expires_at")
    if (
        expires_at
        and expires_at - current < SESSION_WARNING_SECONDS
        and sent.get("session") != expires_at
    ):
        due.append(
            (
                "session",
                expires_at,
                "Your FLOP wallet session is about to expire",
                "Open Wayfinder in your browser to renew it. Until then your agent "
                "cannot sign for its FLOP account.",
            )
        )
    if (
        balance_raw < LOW_BALANCE_RAW
        and current - int(sent.get("low_balance") or 0) > LOW_BALANCE_NOTICE_SECONDS
    ):
        due.append(
            (
                "low_balance",
                current,
                "Your FLOP account is low",
                "It holds less than 1 FLOP. The FLOP job asks the faucet for a grant "
                "once a day when one is due.",
            )
        )
    delivered: list[str] = []
    for kind, marker, title, message in due:
        try:
            await NOTIFY_CLIENT.notify(title, message)
        except httpx.HTTPError:
            continue
        sent[kind] = marker
        delivered.append(kind)
    if delivered:
        state.update_state(notices=sent)
    return delivered


def run_faucet_job() -> None:
    print(json.dumps(asyncio.run(collect_faucet_grant())))


def write_faucet_script() -> Path:
    root = runs_root()
    root.mkdir(parents=True, exist_ok=True)
    path = root / FAUCET_SCRIPT_NAME
    path.write_text(_FAUCET_SCRIPT)
    return path


def ensure_faucet_job() -> dict[str, Any]:
    """Schedule the faucet job with the runner daemon; safe to call repeatedly."""
    script = write_faucet_script()
    paths = get_runner_paths(repo_root=find_repo_root())
    if not paths.sock_path.exists():
        return {
            "scheduled": False,
            "reason": "runner_not_running",
            "hint": "start it with: wayfinder runner start",
        }
    response = RunnerControlClient(sock_path=paths.sock_path).call(
        "add_job",
        {
            "name": FAUCET_JOB_NAME,
            "type": JOB_TYPE_SCRIPT,
            "payload": {"script_path": str(script), "args": []},
            "interval_seconds": FAUCET_CHECK_INTERVAL_SECONDS,
        },
    )
    if response.get("ok"):
        return {"scheduled": True, "created": True, "job": FAUCET_JOB_NAME}
    error = str(response.get("error") or "")
    if "UNIQUE" in error or "exists" in error.lower():
        return {"scheduled": True, "created": False, "job": FAUCET_JOB_NAME}
    return {"scheduled": False, "reason": "runner_error", "error": error}
