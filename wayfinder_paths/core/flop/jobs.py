"""The recurring job for this box's FLOP account: faucet grant and escrow recovery."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx

from wayfinder_paths.core.clients.FlopClient import FLOP_CLIENT
from wayfinder_paths.core.clients.NotifyClient import NOTIFY_CLIENT
from wayfinder_paths.core.flop import faucet, recovery, service, state
from wayfinder_paths.core.flop.account import RAW_PER_FLOP
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

_FAUCET_SCRIPT = '''"""Collects the daily FLOP faucet grant for this box's FLOP account, and closes out any
compute channel a session left open so its escrow comes back.

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
    result = await faucet.collect_if_due(account["flop_address"])
    outcome: dict[str, Any] = {"address": account["flop_address"], **result.as_dict()}
    chain = service.chain_for(account)
    try:
        if recovery.pending_channels():
            outcome["recovered_channels"] = await recovery.recover_channels(
                chain, service.signer_for(account)
            )
        balance_raw = (await chain.balance(account["flop_address"])).free
    finally:
        chain.close()
    outcome["notices"] = await send_due_notices(account, balance_raw)
    return outcome


async def send_due_notices(
    account: dict[str, Any], balance_raw: int, *, now: float | None = None
) -> list[str]:
    """Tell the user, once, about the two things only they can fix."""
    current = int(now if now is not None else time.time())
    sent = dict(state.read_state().get("notices", {}))
    due: list[tuple[str, int, str, str]] = []
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
                "It holds less than 1 FLOP. The faucet job tops it up once a day; you "
                "can also use Get test FLOP in Settings.",
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
