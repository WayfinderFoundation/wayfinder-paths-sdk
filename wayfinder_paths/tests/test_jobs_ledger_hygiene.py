"""Ledger hygiene + standing checks: process rows reroute to the ops ledger,
the research tail stays clean even over legacy files, and routine numbers
are computed mechanically for the wake context."""

from __future__ import annotations

import json

from wayfinder_paths.jobs.ledger import append_ledger_row, tail_ledger
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.worker import _standing_checks_block


def _mk(tmp_path):
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("hygiene-demo", agent_mode="intervene")
    store.save(job)
    return store, job.id


def test_process_families_reroute_to_ops(tmp_path) -> None:
    store, job_id = _mk(tmp_path)
    kept = append_ledger_row(
        store, job_id, "candidates", {"family": "breakout", "name": "donch_up"}
    )
    moved = append_ledger_row(
        store, job_id, "candidates", {"family": "operations", "name": "runner ok"}
    )
    assert kept["family"] == "breakout"
    assert moved["rerouted_from"] == "candidates"
    root = store.job_dir(job_id)
    assert not any(
        "operations" in line
        for line in (root / "ledgers" / "candidates.jsonl").read_text().splitlines()
    )
    ops = (root / "ledgers" / "ops.jsonl").read_text()
    assert "runner ok" in ops


def test_candidates_tail_filters_legacy_process_rows(tmp_path) -> None:
    store, job_id = _mk(tmp_path)
    path = store.job_dir(job_id) / "ledgers" / "candidates.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"family": "monitoring", "name": f"noise-{i}"} for i in range(30)]
    rows.append({"family": "trend", "name": "real-idea", "verdict": "candidate"})
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    tail = tail_ledger(store, job_id, "candidates", limit=5)
    # 30 legacy process rows cannot eat the tail budget: the one research row
    # survives the 5-row limit.
    assert [r["name"] for r in tail] == ["real-idea"]
    # Other ledgers are untouched by the filter.
    append_ledger_row(store, job_id, "decisions", {"family": "monitoring", "d": 1})
    assert len(tail_ledger(store, job_id, "decisions", limit=5)) == 1


def test_standing_checks_block(tmp_path) -> None:
    store, job_id = _mk(tmp_path)
    root = store.job_dir(job_id)
    forward = root / "results" / "forward"
    forward.mkdir(parents=True, exist_ok=True)
    trades = [
        {"symbol": "LIT", "net_pnl": -0.1, "closed_at": "2026-07-26T10:00:00+00:00"},
        {"symbol": "LIT", "net_pnl": 0.2, "closed_at": "2026-07-27T01:00:00+00:00"},
        {"symbol": "XRP", "net_pnl": 0.0, "closed_at": "2026-07-25T09:00:00+00:00"},
    ]
    (forward / "trades.jsonl").write_text(
        "\n".join(json.dumps(t) for t in trades) + "\n", encoding="utf-8"
    )
    (root / "state").mkdir(exist_ok=True)
    feats = [
        {
            "timestamp": "2026-07-27T00:50:00+00:00",
            "name": "regime_code",
            "symbol": "LIT",
            "value": 2.0,
        },
        {
            "timestamp": "2026-07-27T00:00:00+00:00",
            "name": "funding",
            "symbol": "POL",
            "value": -2e-05,
        },
        {
            "timestamp": "2026-07-27T04:00:00+00:00",
            "name": "funding",
            "symbol": "POL",
            "value": -1e-05,
        },
    ]
    (root / "state" / "features.jsonl").write_text(
        "\n".join(json.dumps(f) for f in feats) + "\n", encoding="utf-8"
    )
    block = _standing_checks_block(root)
    assert block["closed_trades"]["total"] == 3
    assert block["closed_trades"]["per_symbol"] == {"LIT": 2, "XRP": 1}
    assert block["regime_now"]["LIT"]["label"] == "down_lowvol"
    pol = block["funding_recent"]["POL"]
    assert pol["n"] == 2 and pol["mean_7d"] == -1.5e-05
    assert pol["newest_ts"] == "2026-07-27T04:00:00+00:00"
    assert "ops ledger" in block["_basis"]

    # Empty job -> empty block.
    job2 = WayfinderJob.new("hygiene-empty", agent_mode="intervene")
    store.save(job2)
    assert _standing_checks_block(store.job_dir(job2.id)) == {}
