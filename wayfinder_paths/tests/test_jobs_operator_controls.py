"""Operator controls: the leverage knob (execution_params.leverage) and its
CLI surface. Mode switching (apply_script_mode) is covered elsewhere; these
pin the new sizing primitive the frontend button drives."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.sync import MAX_OPERATOR_LEVERAGE, apply_execution_leverage


def _job(tmp_path) -> tuple[JobStore, str]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("lev-demo", agent_mode="intervene")
    job.execution_params["leverage"] = 2.0
    store.save(job)
    return store, job.id


def test_apply_execution_leverage_writes_and_journals(tmp_path, monkeypatch) -> None:
    store, job_id = _job(tmp_path)
    synced: list[bool] = []
    monkeypatch.setattr(
        "wayfinder_paths.jobs.sync.sync_all_jobs",
        lambda **kwargs: synced.append(True),
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.background.spawn_detached_op",
        lambda *args: {"started": True},
    )

    result = apply_execution_leverage(job_id, 3.5, store=store)
    assert result["job_id"] == job_id
    assert result["leverage"] == 3.5
    assert result["previous"] == 2.0
    # A changed value kicks the detached gate restamp.
    assert result["restamp"] == {"started": True}
    assert store.load(job_id).execution_params["leverage"] == 3.5
    journal = (store.job_dir(job_id) / "journal.jsonl").read_text(encoding="utf-8")
    rows = [json.loads(line) for line in journal.strip().splitlines()[-2:]]
    types = {row["type"] for row in rows}
    assert types == {"operator_leverage_set", "gate_restamp_kicked"}
    leverage_row = next(r for r in rows if r["type"] == "operator_leverage_set")
    assert leverage_row["from"] == 2.0
    assert leverage_row["to"] == 3.5
    assert synced  # snapshot pushed so backend/frontend see the new value


@pytest.mark.parametrize("bad", [0, -1, MAX_OPERATOR_LEVERAGE + 0.1, "nan"])
def test_apply_execution_leverage_rejects_out_of_range(tmp_path, bad) -> None:
    store, job_id = _job(tmp_path)
    with pytest.raises(ValueError):
        apply_execution_leverage(job_id, float(bad), store=store)
    assert store.load(job_id).execution_params["leverage"] == 2.0


def test_set_leverage_cli_round_trip(tmp_path, monkeypatch) -> None:
    from wayfinder_paths.jobs import cli as cli_module

    store, job_id = _job(tmp_path)
    monkeypatch.setattr(
        cli_module,
        "apply_execution_leverage",
        lambda jid, lev: {"job_id": jid, "leverage": lev, "previous": 2.0},
    )
    runner = CliRunner()
    outcome = runner.invoke(cli_module.job_cli, ["set-leverage", job_id, "4"])
    assert outcome.exit_code == 0, outcome.output
    payload = json.loads(outcome.output)
    assert payload["ok"] is True
    assert payload["result"]["leverage"] == 4.0

    # Out-of-range surfaces as a clean CLI error, not a traceback.
    monkeypatch.setattr(
        cli_module,
        "apply_execution_leverage",
        lambda jid, lev: (_ for _ in ()).throw(ValueError("leverage must be in")),
    )
    outcome = runner.invoke(cli_module.job_cli, ["set-leverage", job_id, "40"])
    assert outcome.exit_code != 0
    assert "leverage must be in" in outcome.output
