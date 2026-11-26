"""The flop_* tools, the service behind them, escrow recovery and the faucet job (no network)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from wayfinder_paths.core import config
from wayfinder_paths.core.flop import faucet, jobs, recovery, service, state
from wayfinder_paths.core.flop.chain import Channel, ChannelView, FlopChainError
from wayfinder_paths.core.flop.networks import TESTNET
from wayfinder_paths.core.flop.session import DirectSessionError
from wayfinder_paths.mcp.tools import flop as tools

ADDRESS = "5CY2922WBXSso1pELFnqQWZy7DeKvLS63P6Cz488X2h1NFzq"
ACCOUNT = {
    "connected": True,
    "network": "testnet",
    "genesis_hash": TESTNET.genesis_hash,
    "flop_address": ADDRESS,
    "account_id": "0x" + "11" * 32,
    "wallet_address": "0x283b000000000000000000000000000000000000",
    "session_expires_at": 1_900_000_000,
    "limits": {"per_call": "5", "daily": "50"},
}
MINER = "5H1qRPoPZAL4wa6VAdcFnzwCUKVEoaMD7UtrW3R9pDBjpsNG"
CHANNEL = "0x" + "ab" * 32


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("WAYFINDER_FLOP_STATE_DIR", str(tmp_path / "flop"))
    monkeypatch.setattr(config, "CONFIG", {"system": {"flop": {}}})
    monkeypatch.setattr(
        service.FLOP_CLIENT, "get_account", AsyncMock(return_value=ACCOUNT)
    )


def _set_flop_config(monkeypatch, **flop):
    monkeypatch.setattr(config, "CONFIG", {"system": {"flop": flop}})


def _http_error(status, body):
    request = httpx.Request("GET", "https://wayfinder.ai/api/v1/flop/account/")
    return httpx.HTTPStatusError(
        "refused",
        request=request,
        response=httpx.Response(status, json=body, request=request),
    )


def _session_result(**overrides):
    values = {
        "reply": "hello",
        "receipts": [SimpleNamespace(prompt_tokens=5, completion_tokens=2)],
        "channel_id": CHANNEL,
        "settlement": "settled",
        "settle_tx": "0xfeed",
        "escrow": 10**16,
        "payable": 10**16,
    }
    return SimpleNamespace(**(values | overrides))


def _fake_chain(monkeypatch, **methods):
    chain = SimpleNamespace(close=lambda: None, **methods)
    monkeypatch.setattr(service, "chain_for", lambda account: chain)
    return chain


# ---- inference routing and caps -----------------------------------------------------


def test_inference_without_a_miner_says_testnet_has_no_gateway():
    result = asyncio.run(tools.flop_infer("ping"))
    assert result["ok"] is False
    assert result["error"]["code"] == "flop_no_miner_endpoint"
    assert "no inference gateway" in result["error"]["message"]


def test_infer_opens_a_channel_from_the_own_account_when_a_miner_is_configured(
    monkeypatch,
):
    _set_flop_config(monkeypatch, miners={MINER: "https://miner.example"})
    _fake_chain(monkeypatch)
    run = AsyncMock(return_value=_session_result())
    monkeypatch.setattr(service, "run_direct_session", run)
    result = asyncio.run(tools.flop_infer("ping", max_tokens=16))
    assert result["ok"]
    assert result["result"]["paid_by"] == "own_account"
    assert result["result"]["channel_id"] == CHANNEL
    kwargs = run.await_args.kwargs
    assert kwargs["miner"] == MINER and kwargs["endpoint"] == "https://miner.example"
    # Testnet lists no prices: each turn offers the fixed escrow, under the per-call cap.
    assert kwargs["max_flop_raw"] == 5 * 10**16
    assert state.spent_today() == 10**16
    logged = state.read_state()["activity"][-1]
    assert logged["kind"] == "inference" and "text" not in logged


def test_direct_escrow_never_exceeds_the_per_call_cap(monkeypatch):
    _set_flop_config(
        monkeypatch, miners={MINER: "https://miner.example"}, per_call_flop="0.01"
    )
    _fake_chain(monkeypatch)
    run = AsyncMock(return_value=_session_result())
    monkeypatch.setattr(service, "run_direct_session", run)
    asyncio.run(tools.flop_infer("ping", max_tokens=4096, max_cost_flop="3"))
    assert run.await_args.kwargs["max_flop_raw"] == 10**16


def test_infer_refuses_once_the_daily_cap_is_spent(monkeypatch):
    _set_flop_config(monkeypatch, daily_flop="0.5")
    state.add_spend(5 * 10**17)
    result = asyncio.run(tools.flop_infer("ping"))
    assert not result["ok"] and result["error"]["code"] == "flop_daily_cap_reached"


def test_a_loopback_miner_endpoint_is_refused_before_anything_is_signed(monkeypatch):
    _set_flop_config(monkeypatch, miners={MINER: "http://127.0.0.1:3096"})
    run = AsyncMock()
    monkeypatch.setattr(service, "run_direct_session", run)
    result = asyncio.run(tools.flop_infer("ping"))
    assert result["error"]["code"] == "flop_miner_endpoint_not_https"
    run.assert_not_awaited()


def test_infer_reports_not_connected(monkeypatch):
    monkeypatch.setattr(
        service.FLOP_CLIENT, "get_account", AsyncMock(return_value={"connected": False})
    )
    result = asyncio.run(tools.flop_infer("ping"))
    assert result["error"]["code"] == "flop_not_connected"


def test_backend_refusals_keep_their_code(monkeypatch):
    monkeypatch.setattr(
        service.FLOP_CLIENT,
        "get_account",
        AsyncMock(
            side_effect=_http_error(
                403, {"error": "FLOP is off", "code": "flop_disabled"}
            )
        ),
    )
    result = asyncio.run(tools.flop_infer("ping"))
    assert result["error"]["code"] == "flop_disabled"


# ---- failed sessions and escrow recovery --------------------------------------------


def test_a_failed_session_is_remembered_for_recovery(monkeypatch):
    _set_flop_config(monkeypatch, miners={MINER: "https://miner.example"})
    _fake_chain(monkeypatch)
    failure = DirectSessionError(
        "miner went quiet", bytes.fromhex("ab" * 32), recovery=None
    )
    monkeypatch.setattr(service, "run_direct_session", AsyncMock(side_effect=failure))
    result = asyncio.run(tools.flop_infer("ping"))
    assert result["error"]["code"] == "flop_session_failed"
    assert result["error"]["details"]["channel_id"] == CHANNEL
    assert CHANNEL in recovery.pending_channels()
    assert state.spent_today() == 0


def test_a_refunded_session_is_not_queued_for_recovery(monkeypatch):
    _set_flop_config(monkeypatch, miners={MINER: "https://miner.example"})
    _fake_chain(monkeypatch)
    failure = DirectSessionError(
        "no ack", bytes.fromhex("ab" * 32), recovery="refunded"
    )
    monkeypatch.setattr(service, "run_direct_session", AsyncMock(side_effect=failure))
    asyncio.run(tools.flop_infer("ping"))
    assert recovery.pending_channels() == {}


def test_a_forced_settlement_is_queued_for_finalize_and_counts_only_the_payable(
    monkeypatch,
):
    _set_flop_config(monkeypatch, miners={MINER: "https://miner.example"})
    _fake_chain(monkeypatch)
    posted = _session_result(settlement="posted", payable=23)
    monkeypatch.setattr(service, "run_direct_session", AsyncMock(return_value=posted))
    asyncio.run(tools.flop_infer("ping"))
    assert recovery.pending_channels()[CHANNEL]["reason"] == "awaiting_finalize"
    assert state.spent_today() == 23


def _channel(state_name):
    return Channel(
        state=state_name, agent=ADDRESS, miner=MINER, agent_key=b"", enclave_key=b"",
        model_hash=b"", measured_root=b"", decode_policy_hash=b"", precision=2, escrow=1,
        opened_at=1, max_duration=1, authored_opened_at=1, snapshot_cap=None, current_cap=None,
    )  # fmt: skip


@pytest.mark.parametrize(
    ("channel_state", "expected_call"),
    [
        ("PendingAck", "expire_force_open"),
        ("Open", "timeout"),
        ("Settling", "finalize"),
    ],
)
def test_recovery_uses_the_call_the_channel_state_allows(channel_state, expected_call):
    recovery.remember_channel(CHANNEL, "session_failed")
    submit = AsyncMock(return_value=SimpleNamespace(extrinsic_hash="0x01"))
    chain = SimpleNamespace(
        channel=AsyncMock(
            return_value=ChannelView(block=9, channel=_channel(channel_state))
        ),
        submit=submit,
    )
    outcomes = asyncio.run(recovery.recover_channels(chain, SimpleNamespace()))
    assert submit.await_args.args[:3] == (
        "ComputeChannel",
        expected_call,
        {"channel_id": CHANNEL},
    )
    assert outcomes == [
        {"channel_id": CHANNEL, "outcome": expected_call, "transaction": "0x01"}
    ]
    assert recovery.pending_channels() == {}


def test_recovery_keeps_a_channel_the_chain_will_not_close_yet():
    recovery.remember_channel(CHANNEL, "session_failed")
    chain = SimpleNamespace(
        channel=AsyncMock(return_value=ChannelView(block=9, channel=_channel("Open"))),
        submit=AsyncMock(
            side_effect=FlopChainError("ComputeChannel.timeout failed: NotExpired")
        ),
    )
    outcomes = asyncio.run(recovery.recover_channels(chain, SimpleNamespace()))
    assert outcomes[0]["outcome"] == "not_yet"
    assert CHANNEL in recovery.pending_channels()


def test_recovery_forgets_a_closed_channel_without_submitting():
    recovery.remember_channel(CHANNEL, "session_failed")
    submit = AsyncMock()
    chain = SimpleNamespace(
        channel=AsyncMock(
            return_value=ChannelView(block=9, channel=_channel("Closed"))
        ),
        submit=submit,
    )
    asyncio.run(recovery.recover_channels(chain, SimpleNamespace()))
    submit.assert_not_awaited()
    assert recovery.pending_channels() == {}


# ---- sub-agent ----------------------------------------------------------------------


def _inference(text):
    return service.Inference(text, "m", "own_account", 1, 1, "0.01")


def test_subagent_drafts_then_reviews_and_stops_when_the_review_agrees(monkeypatch):
    infer = AsyncMock(
        side_effect=[_inference("draft"), _inference("better"), _inference("better")]
    )
    monkeypatch.setattr(service, "infer", infer)
    result = asyncio.run(
        tools.flop_subagent("summarise this", context="the text", max_steps=4)
    )
    assert result["ok"] and result["result"]["answer"] == "better"
    assert infer.await_count == 3
    first_messages = infer.await_args_list[0].args[0]
    assert first_messages[0]["role"] == "system"
    assert "the text" in first_messages[1]["content"]
    review_prompt = infer.await_args_list[1].args[0][1]["content"]
    assert "draft" in review_prompt and "summarise this" in review_prompt
    assert all("text" not in step for step in result["result"]["steps"])


def test_subagent_step_count_is_bounded():
    result = asyncio.run(tools.flop_subagent("task", max_steps=9))
    assert result["error"]["code"] == "invalid_argument"


# ---- connect, faucet job, status ----------------------------------------------------


def test_connect_creates_a_dedicated_strategy_wallet_and_schedules_the_job(monkeypatch):
    get_account = AsyncMock(return_value={"connected": False, "network": "testnet"})
    connect = AsyncMock(return_value=ACCOUNT)
    monkeypatch.setattr(tools.FLOP_CLIENT, "get_account", get_account)
    monkeypatch.setattr(tools.FLOP_CLIENT, "connect", connect)
    monkeypatch.setattr(tools, "load_remote_wallets", AsyncMock(return_value=[]))
    create = AsyncMock(
        return_value={"evm": {"wallet_address": ACCOUNT["wallet_address"]}}
    )
    monkeypatch.setattr(tools, "create_remote_wallet", create)
    monkeypatch.setattr(
        jobs, "ensure_faucet_job", lambda: {"scheduled": True, "created": True}
    )
    monkeypatch.setattr(
        faucet,
        "collect_if_due",
        AsyncMock(return_value=faucet.FaucetResult(granted=True, amount_flop=25)),
    )
    result = asyncio.run(tools.flop_connect())
    assert result["ok"]
    create.assert_awaited_once_with("flop", "strategy")
    connect.assert_awaited_once_with(ACCOUNT["wallet_address"])
    assert result["result"]["faucet_job"]["scheduled"]
    assert result["result"]["faucet"]["granted"]


def test_connect_reuses_an_existing_flop_strategy_wallet(monkeypatch):
    monkeypatch.setattr(
        tools.FLOP_CLIENT, "get_account", AsyncMock(return_value={"connected": False})
    )
    connect = AsyncMock(return_value=ACCOUNT)
    monkeypatch.setattr(tools.FLOP_CLIENT, "connect", connect)
    wallets = [
        {
            "label": "flop",
            "chain_type": "solana",
            "wallet_type": "strategy",
            "address": "So1",
        },
        {
            "label": "flop",
            "chain_type": "ethereum",
            "wallet_type": "session",
            "address": "0xsession",
        },
        {
            "label": "flop",
            "chain_type": "ethereum",
            "wallet_type": "strategy",
            "address": "0xgood",
        },
    ]
    monkeypatch.setattr(tools, "load_remote_wallets", AsyncMock(return_value=wallets))
    create = AsyncMock()
    monkeypatch.setattr(tools, "create_remote_wallet", create)
    monkeypatch.setattr(jobs, "ensure_faucet_job", lambda: {"scheduled": True})
    monkeypatch.setattr(
        faucet,
        "collect_if_due",
        AsyncMock(return_value=faucet.FaucetResult(granted=False, reason="not_due")),
    )
    asyncio.run(tools.flop_connect())
    create.assert_not_awaited()
    connect.assert_awaited_once_with("0xgood")


def test_faucet_job_writes_its_script_and_registers_with_the_runner(
    tmp_path, monkeypatch
):
    runs = tmp_path / "runs"
    sock = tmp_path / "runner.sock"
    sock.touch()
    monkeypatch.setattr(jobs, "runs_root", lambda: runs)
    monkeypatch.setattr(jobs, "find_repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        jobs, "get_runner_paths", lambda repo_root: SimpleNamespace(sock_path=sock)
    )
    calls = []

    class Client:
        def __init__(self, sock_path):
            assert sock_path == sock

        def call(self, method, params):
            calls.append((method, params))
            return (
                {"ok": True}
                if len(calls) == 1
                else {"ok": False, "error": "UNIQUE constraint failed: job_defs.name"}
            )

    monkeypatch.setattr(jobs, "RunnerControlClient", Client)
    assert jobs.ensure_faucet_job() == {
        "scheduled": True,
        "created": True,
        "job": "flop-faucet",
    }
    assert jobs.ensure_faucet_job() == {
        "scheduled": True,
        "created": False,
        "job": "flop-faucet",
    }
    script = runs / "flop_faucet.py"
    assert "run_faucet_job()" in script.read_text()
    method, params = calls[0]
    assert method == "add_job" and params["type"] == "script"
    assert params["interval_seconds"] == 6 * 3600
    assert params["payload"]["script_path"] == str(script)
    compile(script.read_text(), str(script), "exec")


def test_faucet_job_reports_when_the_runner_is_not_running(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "runs_root", lambda: tmp_path / "runs")
    monkeypatch.setattr(jobs, "find_repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        jobs,
        "get_runner_paths",
        lambda repo_root: SimpleNamespace(sock_path=tmp_path / "missing.sock"),
    )
    assert jobs.ensure_faucet_job()["reason"] == "runner_not_running"


def test_job_run_collects_the_grant_and_recovers_pending_channels(monkeypatch):
    monkeypatch.setattr(
        jobs.FLOP_CLIENT, "get_account", AsyncMock(return_value=ACCOUNT)
    )
    monkeypatch.setattr(
        faucet,
        "collect_if_due",
        AsyncMock(return_value=faucet.FaucetResult(granted=True, amount_flop=25)),
    )
    monkeypatch.setattr(jobs.NOTIFY_CLIENT, "notify", AsyncMock())
    recovery.remember_channel(CHANNEL, "session_failed")
    _fake_chain(
        monkeypatch,
        balance=AsyncMock(return_value=SimpleNamespace(free=25 * 10**18, reserved=0)),
        inference_supply=AsyncMock(return_value=(0, 0)),
    )
    recover = AsyncMock(return_value=[{"channel_id": CHANNEL, "outcome": "timeout"}])
    monkeypatch.setattr(recovery, "recover_channels", recover)
    outcome = asyncio.run(jobs.collect_faucet_grant())
    assert outcome["granted"] and outcome["address"] == ADDRESS
    assert outcome["recovered_channels"][0]["outcome"] == "timeout"
    assert outcome["notices"] == []


TESTNET_ACCOUNT = ACCOUNT


def _testnet_job(
    monkeypatch,
    *,
    supply,
    balance=10 * 10**18,
    miners=None,
    miner_active=True,
    endpoint_up=True,
    model_registered=True,
):
    monkeypatch.setattr(
        jobs.FLOP_CLIENT, "get_account", AsyncMock(return_value=TESTNET_ACCOUNT)
    )
    monkeypatch.setattr(
        service.FLOP_CLIENT, "get_account", AsyncMock(return_value=TESTNET_ACCOUNT)
    )
    collect = AsyncMock(return_value=faucet.FaucetResult(granted=True, amount_flop=10))
    monkeypatch.setattr(faucet, "collect_if_due", collect)
    notify = AsyncMock()
    monkeypatch.setattr(jobs.NOTIFY_CLIENT, "notify", notify)
    resolve = AsyncMock(
        side_effect=None
        if model_registered
        else FlopChainError("not exactly one verified model")
    )
    _fake_chain(
        monkeypatch,
        balance=AsyncMock(return_value=SimpleNamespace(free=balance, reserved=0)),
        inference_supply=AsyncMock(return_value=supply),
        resolve_model_identity=resolve,
        miner_is_active=AsyncMock(return_value=miner_active),
    )
    monkeypatch.setattr(jobs, "endpoint_answers", AsyncMock(return_value=endpoint_up))
    if miners:
        _set_flop_config(monkeypatch, miners=miners)
    session = AsyncMock(return_value=_session_result(payable=3 * 10**16))
    monkeypatch.setattr(service, "run_direct_session", session)
    return collect, notify, session


def test_testnet_job_claims_with_the_testnet_profile_and_waits_for_inference(
    monkeypatch,
):
    collect, notify, session = _testnet_job(
        monkeypatch, supply=(0, 0), miners={MINER: "https://miner.example"}
    )
    outcome = asyncio.run(jobs.collect_faucet_grant())
    signer, network = collect.await_args.args
    assert network is TESTNET and signer.account_id == bytes.fromhex("11" * 32)
    assert outcome["network"] == "testnet"
    assert outcome["spend"] == {"spent": False, "reason": "testnet_inference_not_open"}
    session.assert_not_awaited()
    notify.assert_not_awaited()


def test_testnet_job_spends_once_a_day_when_miners_are_live(monkeypatch):
    _, notify, session = _testnet_job(
        monkeypatch, supply=(3, 2), miners={MINER: "https://miner.example"}
    )
    outcome = asyncio.run(jobs.collect_faucet_grant())
    assert outcome["spend"]["spent"] and outcome["spend"]["cost_flop"] == "0.03"
    assert session.await_args.kwargs["miner"] == MINER
    # The spend uses the network's own default model.
    assert session.await_args.kwargs["model"] == "Qwen/Qwen3-14B"
    assert state.last_spend_at() > 0
    again = asyncio.run(jobs.collect_faucet_grant())
    assert again["spend"] == {"spent": False, "reason": "spent_recently"}
    assert session.await_count == 1
    # Told once that testnet inference opened.
    assert [c.args[0] for c in notify.await_args_list] == [
        "FLOP testnet now sells inference"
    ]


@pytest.mark.parametrize(
    ("balance", "miners", "reason"),
    [
        (10 * 10**18, None, "no_miner_endpoint"),
        (10**17, {MINER: "https://miner.example"}, "balance_below_floor"),
    ],
)
def test_testnet_job_does_not_spend_without_a_miner_or_a_balance(
    monkeypatch, balance, miners, reason
):
    _, _, session = _testnet_job(
        monkeypatch, supply=(1, 1), balance=balance, miners=miners
    )
    outcome = asyncio.run(jobs.collect_faucet_grant())
    assert outcome["spend"] == {"spent": False, "reason": reason}
    session.assert_not_awaited()


@pytest.mark.parametrize("supply", [(3, 0), (0, 2)])
def test_testnet_job_needs_both_miners_and_models(monkeypatch, supply):
    _, notify, session = _testnet_job(
        monkeypatch, supply=supply, miners={MINER: "https://miner.example"}
    )
    outcome = asyncio.run(jobs.collect_faucet_grant())
    assert outcome["spend"] == {"spent": False, "reason": "testnet_inference_not_open"}
    session.assert_not_awaited()
    notify.assert_not_awaited()


@pytest.mark.parametrize(
    ("check", "skipped"),
    [
        ({"miner_active": False}, {MINER: "miner_not_active"}),
        ({"endpoint_up": False}, {MINER: "miner_unreachable"}),
        ({"model_registered": False}, {"*": "spend_model_not_registered"}),
    ],
)
def test_testnet_job_does_not_spend_on_a_miner_that_cannot_serve(
    monkeypatch, check, skipped
):
    _, _, session = _testnet_job(
        monkeypatch, supply=(3, 2), miners={MINER: "https://miner.example"}, **check
    )
    outcome = asyncio.run(jobs.collect_faucet_grant())
    assert outcome["spend"] == {
        "spent": False,
        "reason": "no_ready_miner",
        "skipped": skipped,
    }
    session.assert_not_awaited()
    assert state.last_spend_at() == 0


def test_inference_defaults_to_the_strongest_testnet_model(monkeypatch):
    model = "Qwen/Qwen3-14B"
    _set_flop_config(monkeypatch, miners={MINER: "https://miner.example"})
    _fake_chain(monkeypatch)
    session = AsyncMock(return_value=_session_result(payable=3 * 10**16))
    monkeypatch.setattr(service, "run_direct_session", session)
    result = asyncio.run(tools.flop_infer("ping"))
    assert result["ok"], result
    assert session.await_args.kwargs["model"] == model
    assert result["result"]["model"] == model


def test_job_warns_once_about_an_expiring_session_and_a_low_balance(monkeypatch):
    notify = AsyncMock()
    monkeypatch.setattr(jobs.NOTIFY_CLIENT, "notify", notify)
    now = ACCOUNT["session_expires_at"] - 3600
    assert asyncio.run(jobs.send_due_notices(ACCOUNT, 10**17, now=now)) == [
        "session",
        "low_balance",
    ]
    assert asyncio.run(jobs.send_due_notices(ACCOUNT, 10**17, now=now + 60)) == []
    assert notify.await_count == 2
    renewed = ACCOUNT | {
        "session_expires_at": ACCOUNT["session_expires_at"] + 7 * 86400
    }
    assert asyncio.run(jobs.send_due_notices(renewed, 5 * 10**18, now=now + 120)) == []


def test_a_notice_that_fails_to_send_is_tried_again_next_run(monkeypatch):
    request = httpx.Request("POST", "https://wayfinder.ai/api/v1/opencode/notify/")
    notify = AsyncMock(side_effect=[httpx.ConnectError("down", request=request), None])
    monkeypatch.setattr(jobs.NOTIFY_CLIENT, "notify", notify)
    now = ACCOUNT["session_expires_at"] - 3600
    assert asyncio.run(jobs.send_due_notices(ACCOUNT, 5 * 10**18, now=now)) == []
    assert asyncio.run(jobs.send_due_notices(ACCOUNT, 5 * 10**18, now=now + 60)) == [
        "session"
    ]


def test_job_run_does_nothing_when_no_account_is_connected(monkeypatch):
    monkeypatch.setattr(
        jobs.FLOP_CLIENT, "get_account", AsyncMock(return_value={"connected": False})
    )
    collect = AsyncMock()
    monkeypatch.setattr(faucet, "collect_if_due", collect)
    assert asyncio.run(jobs.collect_faucet_grant())["reason"] == "flop_not_connected"
    collect.assert_not_awaited()


def test_status_reports_balance_caps_and_the_faucet_job(monkeypatch):
    monkeypatch.setattr(
        tools.FLOP_CLIENT, "get_account", AsyncMock(return_value=ACCOUNT)
    )
    _fake_chain(
        monkeypatch,
        balance=AsyncMock(return_value=SimpleNamespace(free=25 * 10**18, reserved=0)),
    )
    monkeypatch.setattr(
        jobs, "ensure_faucet_job", lambda: {"scheduled": True, "created": False}
    )
    result = asyncio.run(tools.flop_status())
    assert result["ok"] and result["result"]["balance_flop"] == "25"
    assert result["result"]["network"] == "testnet"
    assert result["result"]["faucet_job"]["scheduled"]
    assert result["result"]["direct_miners_configured"] == 0
    assert "gateway_key" not in result["result"]


def test_models_lists_the_verified_registry_and_marks_the_default(monkeypatch):
    _fake_chain(
        monkeypatch,
        verified_models=AsyncMock(
            return_value=["Qwen/Qwen3-14B", "Qwen/Qwen3-30B-A3B-FP8"]
        ),
    )
    result = asyncio.run(tools.flop_models())
    assert result["result"] == [
        {"id": "Qwen/Qwen3-14B", "default": True},
        {"id": "Qwen/Qwen3-30B-A3B-FP8", "default": False},
    ]


@pytest.mark.parametrize(
    ("config", "spent", "cap", "expected"),
    [
        ({}, 0, None, "0.05"),
        ({}, 0, "0.01", "0.01"),
        ({"per_call_flop": "0.02"}, 0, None, "0.02"),
        ({"daily_flop": "1"}, 99 * 10**16, None, "0.01"),
    ],
)
def test_estimate_is_the_escrow_one_call_offers(
    monkeypatch, config, spent, cap, expected
):
    _set_flop_config(monkeypatch, **config)
    if spent:
        state.add_spend(spent)
    result = asyncio.run(tools.flop_estimate(**({"max_cost_flop": cap} if cap else {})))
    assert result["result"] == {"max_cost_flop": expected}


def test_flop_tools_are_registered_on_an_instance(monkeypatch):
    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "test-instance")
    from wayfinder_paths.mcp.server import build_mcp

    names = {tool.name for tool in build_mcp()._tool_manager.list_tools()}
    assert {
        "flop_status", "flop_connect", "flop_models", "flop_estimate",
        "flop_infer", "flop_faucet", "flop_activity", "flop_subagent",
    } <= names  # fmt: skip
