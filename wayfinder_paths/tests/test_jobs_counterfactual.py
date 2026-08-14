"""Post-apply three-book prospective: replay the pre-apply strategy (A) and
the promoted strategy (B) over the forward window, diff against the actual
book (C) — strategy effect (B-A) split from execution effect (C-B)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd
import yaml

from wayfinder_paths.jobs.counterfactual import (
    COUNTERFACTUAL_PATH,
    counterfactual_job,
    load_counterfactual,
)
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore

_SPEC = {
    "data_contract": {"bar_interval": "5m", "symbols": ["LIT"]},
    "venues": ["hyperliquid"],
}


def _mk_job(tmp_path, *, applied_hours_ago: float = 24.0) -> tuple[JobStore, str]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("cf-demo", agent_mode="intervene")
    store.save(job)
    root = store.job_dir(job.id)

    job_yaml = {
        "id": job.id,
        "script_loop": {"enabled": True, "entrypoint": "workspace/src/strategy.py"},
        "execution_spec": _SPEC,
        "execution_params": {"symbols": ["LIT"]},
    }
    (root / "job.yaml").write_text(yaml.safe_dump(job_yaml), encoding="utf-8")

    backup = root / "applications" / "prop-x" / "backup"
    (backup / "workspace" / "src").mkdir(parents=True)
    (backup / "workspace" / "src" / "strategy.py").write_text(
        "def build_strategy():\n    raise NotImplementedError\n", encoding="utf-8"
    )
    (backup / "job.yaml").write_text(yaml.safe_dump(job_yaml), encoding="utf-8")

    # The promoted revision lives in the ACTIVE workspace — book B's script.
    active_src = root / "workspace" / "src"
    active_src.mkdir(parents=True, exist_ok=True)
    (active_src / "strategy.py").write_text(
        "def build_strategy():\n    raise NotImplementedError\n", encoding="utf-8"
    )

    applied = pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=applied_hours_ago)
    versions = root / "versions"
    versions.mkdir(exist_ok=True)
    (versions / "revisions.jsonl").write_text(
        json.dumps(
            {"ts": applied.isoformat(), "revision": "rev-new", "proposal_id": "prop-x"}
        )
        + "\n",
        encoding="utf-8",
    )
    return store, job.id


def _bars(*, count: int = 1100, interval_s: int = 300) -> list[dict]:
    end = pd.Timestamp.now(tz="UTC").floor("5min")
    rows = []
    for i in range(count):
        ts = end - pd.Timedelta(seconds=interval_s * (count - i))
        rows.append(
            {
                "timestamp": ts.isoformat(),
                "symbol": "LIT",
                "open": 2.0,
                "high": 2.1,
                "low": 1.9,
                "close": 2.0,
                "volume": 10.0,
            }
        )
    return rows


def _fill(ts: pd.Timestamp, *, side: str, reduce_only: bool, pnl: float = 0.0) -> dict:
    return {
        "symbol": "LIT",
        "side": side,
        "reduce_only": reduce_only,
        "status": "filled",
        "filled_size": 1.0,
        "avg_price": 2.0,
        "timestamp": ts.isoformat(),
        "realized_pnl_delta": pnl,
    }


def test_counterfactual_diffs_shadow_against_actual(tmp_path) -> None:
    store, job_id = _mk_job(tmp_path)
    root = store.job_dir(job_id)
    now = pd.Timestamp.now(tz="UTC").floor("5min")
    t_shared = now - pd.Timedelta(hours=6)
    t_skipped = now - pd.Timedelta(hours=4)
    t_unfilled = now - pd.Timedelta(hours=2)

    # Shadow A (pre-apply strategy) trades BOTH entries; the live book only
    # took the shared one — t_skipped is what the change suppressed, and its
    # shadow close won +0.9.
    shadow_trades = [
        _fill(t_shared, side="sell", reduce_only=False),
        _fill(
            t_shared + pd.Timedelta(minutes=80), side="buy", reduce_only=True, pnl=-0.2
        ),
        _fill(t_skipped, side="sell", reduce_only=False),
        _fill(
            t_skipped + pd.Timedelta(minutes=80), side="buy", reduce_only=True, pnl=0.9
        ),
    ]
    # Shadow B (promoted strategy, simulated) takes the shared entry plus one
    # at t_unfilled that the live book never printed — an execution miss.
    active_trades = [
        _fill(t_shared, side="sell", reduce_only=False),
        _fill(
            t_shared + pd.Timedelta(minutes=80), side="buy", reduce_only=True, pnl=-0.2
        ),
        _fill(t_unfilled, side="sell", reduce_only=False),
        _fill(
            t_unfilled + pd.Timedelta(minutes=40), side="buy", reduce_only=True, pnl=0.3
        ),
    ]
    forward = root / "results" / "forward"
    forward.mkdir(parents=True, exist_ok=True)
    (forward / "fills.jsonl").write_text(
        json.dumps(_fill(t_shared, side="sell", reduce_only=False)) + "\n",
        encoding="utf-8",
    )
    (forward / "trades.jsonl").write_text(
        json.dumps(
            {
                "symbol": "LIT",
                "side": "buy",
                "price": 2.02,
                "net_pnl": -0.25,
                "closed_at": (t_shared + pd.Timedelta(minutes=80)).isoformat(),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    captured: dict = {}

    def fake_fetch(**kwargs) -> list[dict]:
        captured["lookback"] = kwargs["lookback_bars"]
        return _bars()

    def fake_sim(script, dataset, spec, params):
        captured.setdefault("scripts", []).append(str(script))
        if "backup" in str(script):
            return SimpleNamespace(trades=shadow_trades)
        return SimpleNamespace(trades=active_trades)

    doc = counterfactual_job(
        job_id, store=store, fetch_bars=fake_fetch, simulate=fake_sim
    )
    assert doc["available"] is True
    assert doc["proposal_id"] == "prop-x"
    # Both books simulated: A from the rollback backup, B from the active
    # workspace, over the same dataset.
    assert any("applications/prop-x/backup" in s for s in captured["scripts"])
    assert any("backup" not in s for s in captured["scripts"])

    assert doc["actual"] == {"closes": 1, "net_pnl": -0.25}
    assert doc["shadow"] == {"closes": 2, "net_pnl": 0.7}
    assert doc["active_shadow"] == {"closes": 2, "net_pnl": 0.1}
    assert doc["delta_net_pnl"] == -0.95
    # Three-book split: the change itself cost -0.6 (B-A); execution lost a
    # further -0.35 (C-B); the two sum to the two-book delta.
    assert doc["effects"] == {
        "strategy_effect": -0.6,
        "execution_effect": -0.35,
        "total_delta": -0.95,
    }
    assert doc["entries_skipped_by_change"]["count"] == 1
    assert doc["entries_skipped_by_change"]["examples"][0]["side"] == "short"
    assert doc["entries_added_by_change"]["count"] == 0
    assert doc["entries_execution_missed"]["count"] == 1
    assert doc["entries_execution_extra"]["count"] == 0
    assert doc["by_symbol"]["LIT"]["shadow_closes"] == 2
    assert doc["by_symbol"]["LIT"]["active_shadow_closes"] == 2
    assert doc["by_symbol"]["LIT"]["active_shadow_net_pnl"] == 0.1

    # Artifact persisted and served from cache while inputs are unchanged.
    assert load_counterfactual(store, job_id)["delta_net_pnl"] == -0.95
    captured["scripts"] = []
    again = counterfactual_job(
        job_id, store=store, fetch_bars=fake_fetch, simulate=fake_sim
    )
    assert again["computed_at"] == doc["computed_at"]
    assert captured["scripts"] == []  # sims were not re-run

    # A new actual close invalidates the fingerprint and recomputes.
    with (forward / "trades.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "symbol": "LIT",
                    "side": "buy",
                    "price": 2.0,
                    "net_pnl": 0.1,
                    "closed_at": now.isoformat(),
                }
            )
            + "\n"
        )
    recomputed = counterfactual_job(
        job_id, store=store, fetch_bars=fake_fetch, simulate=fake_sim
    )
    assert recomputed["actual"]["closes"] == 2
    assert len(captured["scripts"]) == 2  # both books re-simulated


def test_counterfactual_unavailable_paths(tmp_path) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("cf-empty", agent_mode="intervene")
    store.save(job)
    doc = counterfactual_job(job.id, store=store)
    assert doc["available"] is False
    assert "no promoted proposal" in doc["reason"]

    # Too fresh: applied minutes ago -> refuse to compare noise.
    store2, job2 = _mk_job(tmp_path / "j2", applied_hours_ago=0.1)
    doc2 = counterfactual_job(job2, store=store2)
    assert doc2["available"] is False
    assert "too fresh" in doc2["reason"]

    # Simulator blowing up must journal + degrade, never raise.
    store3, job3 = _mk_job(tmp_path / "j3")

    def boom(**kwargs):
        raise RuntimeError("venue down")

    doc3 = counterfactual_job(job3, store=store3, fetch_bars=boom)
    assert doc3["available"] is False
    journal = (store3.job_dir(job3) / "journal.jsonl").read_text(encoding="utf-8")
    assert "counterfactual_failed" in journal


def test_worker_block_renders_topline(tmp_path) -> None:
    from wayfinder_paths.jobs.worker import _counterfactual_block

    store, job_id = _mk_job(tmp_path)
    store.write_json(
        job_id,
        COUNTERFACTUAL_PATH,
        {
            "available": True,
            "proposal_id": "prop-x",
            "applied_at": "2026-07-26T11:02:00+00:00",
            "window": {"days": 3.0},
            "actual": {"closes": 4, "net_pnl": -0.5},
            "shadow": {"closes": 6, "net_pnl": 0.4},
            "active_shadow": {"closes": 5, "net_pnl": -0.1},
            "delta_net_pnl": -0.9,
            "effects": {
                "strategy_effect": -0.5,
                "execution_effect": -0.4,
                "total_delta": -0.9,
            },
            "by_symbol": {},
            "entries_skipped_by_change": {"count": 2, "examples": []},
            "entries_added_by_change": {"count": 0, "examples": []},
            "entries_execution_missed": {"count": 1, "examples": []},
            "entries_execution_extra": {"count": 0, "examples": []},
            "_basis": "note",
            "computed_at": pd.Timestamp.now(tz="UTC").isoformat(),
            "fingerprint": {"revision": "rev-new", "actual_closes_total": 0},
        },
    )
    block = _counterfactual_block(store, job_id)
    assert block["delta_net_pnl"] == -0.9
    assert block["proposal_id"] == "prop-x"
    assert block["effects"]["execution_effect"] == -0.4
    assert block["active_shadow"]["net_pnl"] == -0.1
    assert block["entries_execution_missed"]["count"] == 1
    assert "fingerprint" not in block and "computed_at" not in block
