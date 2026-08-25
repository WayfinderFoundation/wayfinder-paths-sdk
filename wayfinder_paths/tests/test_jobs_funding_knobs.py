"""Operator funding knobs: wallet_label (which funded wallet a live job
trades from) and initial_capital (the equity base live sizing compounds
from) + their CLI surfaces. Both are revision-hash-excluded routing/
accounting fields — pinned here so binding a wallet or recording a deposit
never orphans the gate stamps."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from wayfinder_paths.jobs.gating import compute_workspace_revision
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.sync import apply_initial_capital, apply_wallet_label


def _job(tmp_path) -> tuple[JobStore, str]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("fund-demo", agent_mode="intervene")
    job.execution_params["initial_capital"] = 10_000.0
    store.save(job)
    return store, job.id


def test_apply_wallet_label_writes_journals_and_keeps_revision(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _job(tmp_path)
    synced: list[bool] = []
    monkeypatch.setattr(
        "wayfinder_paths.jobs.sync.sync_all_jobs",
        lambda **kwargs: synced.append(True),
    )
    before = compute_workspace_revision(store.job_dir(job_id))

    result = apply_wallet_label(job_id, "fund-demo", store=store)
    assert result == {
        "job_id": job_id,
        "wallet_label": "fund-demo",
        "previous": None,
    }
    assert store.load(job_id).execution_params["wallet_label"] == "fund-demo"
    assert synced == [True]
    # Routing knob: the gate stamps must survive the bind.
    assert compute_workspace_revision(store.job_dir(job_id)) == before
    journal = (store.job_dir(job_id) / "journal.jsonl").read_text(encoding="utf-8")
    row = json.loads(journal.strip().splitlines()[-1])
    assert row["type"] == "operator_wallet_label_set"
    assert row["to"] == "fund-demo"


def test_apply_wallet_label_rejects_empty(tmp_path) -> None:
    store, job_id = _job(tmp_path)
    with pytest.raises(ValueError):
        apply_wallet_label(job_id, "   ", store=store)


def test_apply_initial_capital_writes_journals_and_keeps_revision(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _job(tmp_path)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.sync.sync_all_jobs", lambda **kwargs: None
    )
    before = compute_workspace_revision(store.job_dir(job_id))

    result = apply_initial_capital(job_id, 250.0, store=store)
    assert result == {
        "job_id": job_id,
        "initial_capital": 250.0,
        "previous": 10_000.0,
    }
    assert store.load(job_id).execution_params["initial_capital"] == 250.0
    assert compute_workspace_revision(store.job_dir(job_id)) == before
    journal = (store.job_dir(job_id) / "journal.jsonl").read_text(encoding="utf-8")
    row = json.loads(journal.strip().splitlines()[-1])
    assert row["type"] == "operator_initial_capital_set"
    assert row["from"] == 10_000.0
    assert row["to"] == 250.0


def test_apply_initial_capital_zero_ok_negative_rejected(tmp_path, monkeypatch) -> None:
    store, job_id = _job(tmp_path)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.sync.sync_all_jobs", lambda **kwargs: None
    )
    # Full withdrawal: zero is the honest "unfunded" state, allowed.
    assert apply_initial_capital(job_id, 0, store=store)["initial_capital"] == 0.0
    with pytest.raises(ValueError):
        apply_initial_capital(job_id, -1, store=store)


def test_funding_knob_clis_round_trip(tmp_path, monkeypatch) -> None:
    from wayfinder_paths.jobs import cli as cli_module

    store, job_id = _job(tmp_path)
    monkeypatch.setattr(
        cli_module,
        "apply_wallet_label",
        lambda jid, label: {"job_id": jid, "wallet_label": label, "previous": None},
    )
    monkeypatch.setattr(
        cli_module,
        "apply_initial_capital",
        lambda jid, amount: {
            "job_id": jid,
            "initial_capital": amount,
            "previous": None,
        },
    )
    runner = CliRunner()

    outcome = runner.invoke(
        cli_module.job_cli, ["set-wallet-label", job_id, "fund-demo"]
    )
    assert outcome.exit_code == 0, outcome.output
    assert json.loads(outcome.output)["result"]["wallet_label"] == "fund-demo"

    outcome = runner.invoke(cli_module.job_cli, ["set-initial-capital", job_id, "75.5"])
    assert outcome.exit_code == 0, outcome.output
    assert json.loads(outcome.output)["result"]["initial_capital"] == 75.5

    # Errors surface as clean CLI errors, not tracebacks.
    monkeypatch.setattr(
        cli_module,
        "apply_wallet_label",
        lambda jid, label: (_ for _ in ()).throw(ValueError("wallet label")),
    )
    outcome = runner.invoke(cli_module.job_cli, ["set-wallet-label", job_id, " "])
    assert outcome.exit_code != 0
    assert "wallet label" in outcome.output


def test_venue_deposit_bridges_then_grows_capital(tmp_path, monkeypatch) -> None:
    import asyncio

    from wayfinder_paths.jobs import sync as sync_module

    store, job_id = _job(tmp_path)
    job = store.load(job_id)
    job.execution_params["wallet_label"] = job_id
    store.save(job)
    monkeypatch.setattr(sync_module, "sync_all_jobs", lambda **kwargs: None)
    calls: list[dict] = []

    async def fake_deposit(*, wallet_label, amount_usdc):
        calls.append({"wallet_label": wallet_label, "amount_usdc": amount_usdc})
        return {"ok": True, "result": {"status": "confirmed"}}

    import wayfinder_paths.mcp.tools.hyperliquid as hl

    monkeypatch.setattr(hl, "hyperliquid_deposit_usdc", fake_deposit)

    result = asyncio.run(sync_module.venue_deposit(job_id, 25.0, store=store))
    assert calls == [{"wallet_label": job_id, "amount_usdc": 25.0}]
    assert result["deposit_status"] == "confirmed"
    # First venue deposit REPLACES the paper-default capital...
    assert result["initial_capital"] == 25.0
    assert store.load(job_id).execution_params["initial_capital"] == 25.0
    # ...and later deposits add.
    again = asyncio.run(sync_module.venue_deposit(job_id, 10.0, store=store))
    assert again["initial_capital"] == 35.0


def test_venue_deposit_failed_send_never_touches_capital(tmp_path, monkeypatch) -> None:
    import asyncio

    from wayfinder_paths.jobs import sync as sync_module

    store, job_id = _job(tmp_path)
    job = store.load(job_id)
    job.execution_params["wallet_label"] = job_id
    store.save(job)

    async def fake_deposit(*, wallet_label, amount_usdc):
        return {"ok": True, "result": {"status": "failed", "error": "no gas"}}

    import wayfinder_paths.mcp.tools.hyperliquid as hl

    monkeypatch.setattr(hl, "hyperliquid_deposit_usdc", fake_deposit)

    with pytest.raises(ValueError):
        asyncio.run(sync_module.venue_deposit(job_id, 25.0, store=store))
    assert store.load(job_id).execution_params["initial_capital"] == 10_000.0


def test_venue_withdraw_shrinks_capital_floored_at_zero(tmp_path, monkeypatch) -> None:
    import asyncio

    from wayfinder_paths.jobs import sync as sync_module

    store, job_id = _job(tmp_path)
    job = store.load(job_id)
    job.execution_params["wallet_label"] = job_id
    job.execution_params["initial_capital"] = 50.0
    store.save(job)
    monkeypatch.setattr(sync_module, "sync_all_jobs", lambda **kwargs: None)

    async def fake_withdraw(*, wallet_label, amount_usdc):
        return {"ok": True, "result": {"status": "confirmed"}}

    import wayfinder_paths.mcp.tools.hyperliquid as hl

    monkeypatch.setattr(hl, "hyperliquid_withdraw_usdc", fake_withdraw)

    result = asyncio.run(sync_module.venue_withdraw(job_id, 80.0, store=store))
    assert result["initial_capital"] == 0.0


def test_venue_ops_require_bound_wallet(tmp_path) -> None:
    import asyncio

    from wayfinder_paths.jobs import sync as sync_module

    store, job_id = _job(tmp_path)
    with pytest.raises(ValueError, match="wallet"):
        asyncio.run(sync_module.venue_deposit(job_id, 25.0, store=store))
