from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from wayfinder_paths.jobs.candidate_shadow import _target_shadow_state_root
from wayfinder_paths.jobs.execution.driver import _apply_symbol_entry_blocks
from wayfinder_paths.jobs.execution.engine import EngineState
from wayfinder_paths.jobs.execution.primitives import (
    FillEvent,
    OrderIntent,
    RestingOrder,
)
from wayfinder_paths.jobs.gating import compute_workspace_revision
from wayfinder_paths.jobs.halt import clear_halt, read_halt
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.probation import (
    ensure_unified_probation,
    load_probation,
    maybe_adjudicate_probation,
    stage_evolution_probation,
)
from wayfinder_paths.jobs.risk_overrides import (
    active_symbol_blocks,
    enforced_symbol_blocks,
    risk_block_symbol,
    risk_unblock_symbol,
)
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.sync import snapshot_job


def _job(tmp_path: Path) -> tuple[JobStore, str]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "majors-5m-lab",
        script="workspace/src/strategy.py",
        agent_mode="intervene",
        execution_contract="jobs_v1",
    )
    job.execution_params = {"symbols": ["BTC", "HYPE"], "venue": "hyperliquid"}
    store.save(job)
    script = store.job_dir(job.id) / "workspace/src/strategy.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("def decide(ctx):\n    return []\n", encoding="utf-8")
    return store, job.id


def _candidate(store: JobStore, job_id: str, name: str) -> tuple[Path, str]:
    root = store.job_dir(job_id)
    candidate = root / "research" / "candidates" / name
    (candidate / "workspace/src").mkdir(parents=True)
    (candidate / "workspace/src/strategy.py").write_text(
        f"# {name}\ndef decide(ctx):\n    return []\n", encoding="utf-8"
    )
    (candidate / "job.yaml").write_bytes((root / "job.yaml").read_bytes())
    return candidate, compute_workspace_revision(candidate)


def _write_ticks(path: Path, stamps: list[datetime]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "ticks.jsonl").write_text(
        "".join(json.dumps({"bar_ts": stamp.isoformat()}) + "\n" for stamp in stamps),
        encoding="utf-8",
    )


def _write_trade(path: Path, stamp: datetime, pnl: float) -> None:
    with (path / "trades.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"timestamp": stamp.isoformat(), "net_pnl": pnl}) + "\n"
        )


def test_active_c03_experiment_migrates_without_resetting_clock_or_cursors(
    tmp_path: Path,
) -> None:
    store, job_id = _job(tmp_path)
    started = datetime(2026, 8, 30, 23, 20, tzinfo=UTC)
    ends = datetime(2026, 9, 13, 23, 20, tzinfo=UTC)
    candidate_cursor = datetime(2026, 8, 31, 4, tzinfo=UTC).isoformat()
    reference_cursor = datetime(2026, 8, 31, 3, 55, tzinfo=UTC).isoformat()
    experiment = {
        "experiment_id": "paper-ab-20260830T232000Z",
        "status": "active",
        "started_at": started.isoformat(),
        "ends_at": ends.isoformat(),
        "confidence": 0.9,
        "protocol": {"paper_only": True},
        "arms": {
            "evolution": {
                "last_processed_bar": candidate_cursor,
                "champion": {
                    "candidate_id": "20260828T160925Z-bbd5e625-c03-30411dca",
                    "revision": "649eb244b94f",
                    "source": "evolution_campaign",
                    "admitted_at": started.isoformat(),
                    "bundle": (
                        "research/evolution/experiment/paper-ab-20260830T232000Z/"
                        "evolution/649eb244b94f"
                    ),
                    "stream": ("results/forward/experiment/evolution/649eb244b94f-a1"),
                },
            },
            "control": {
                "last_processed_bar": reference_cursor,
                "champion": {
                    "candidate_id": "incumbent-bbd5e62584fe",
                    "revision": "bbd5e62584fe",
                    "source": "incumbent",
                    "bundle": (
                        "research/evolution/experiment/paper-ab-20260830T232000Z/"
                        "control/bbd5e62584fe"
                    ),
                    "stream": ("results/forward/experiment/control/bbd5e62584fe-a1"),
                },
            },
        },
    }
    store.write_json(job_id, "state/evolution_experiment.json", experiment)

    doc = ensure_unified_probation(
        store, job_id, now=datetime(2026, 8, 31, 5, tzinfo=UTC)
    )

    assert len(doc["trials"]) == 1
    trial = doc["trials"][0]
    assert trial["candidate_id"] == "20260828T160925Z-bbd5e625-c03-30411dca"
    assert trial["status"] == "active"
    assert trial["phase"] == "forward"
    assert trial["forward"]["started_at"] == started.isoformat()
    assert trial["forward"]["deadline_at"] == ends.isoformat()
    assert trial["candidate"]["last_processed_bar"] == candidate_cursor
    assert trial["reference"]["last_processed_bar"] == reference_cursor
    root = store.job_dir(job_id)
    assert (
        _target_shadow_state_root(root, trial["candidate"])
        == (root / "state/evolution_shadows/evolution/champion/649eb244b94f").resolve()
    )
    assert (
        _target_shadow_state_root(root, trial["reference"])
        == (root / "state/evolution_shadows/control/champion/bbd5e62584fe").resolve()
    )
    archived = store.read_json(job_id, "state/evolution_experiment.json")
    assert archived["status"] == "migrated"
    assert archived["migrated_to_probation"] == trial["trial_id"]


def test_legacy_migration_recovers_between_writes_without_duplicate_trial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job_id = _job(tmp_path)
    started = datetime(2026, 8, 30, tzinfo=UTC)
    experiment = {
        "experiment_id": "paper-ab-crash-test",
        "status": "active",
        "started_at": started.isoformat(),
        "ends_at": (started + timedelta(days=14)).isoformat(),
        "arms": {
            "evolution": {
                "champion": {
                    "candidate_id": "c03",
                    "revision": "candidate-revision",
                    "source": "evolution_campaign",
                    "bundle": "research/evolution/experiment/test/evolution/c03",
                    "stream": "results/forward/experiment/evolution/c03",
                }
            },
            "control": {
                "champion": {
                    "candidate_id": "incumbent",
                    "revision": "incumbent-revision",
                    "source": "incumbent",
                    "bundle": "research/evolution/experiment/test/control/incumbent",
                    "stream": "results/forward/experiment/control/incumbent",
                }
            },
        },
    }
    store.write_json(job_id, "state/evolution_experiment.json", experiment)
    original_write = store.write_json

    def fail_legacy_retirement(job: str, relative: str, data: object) -> Path:
        if relative == "state/evolution_experiment.json":
            raise OSError("simulated crash after probation write")
        return original_write(job, relative, data)

    monkeypatch.setattr(store, "write_json", fail_legacy_retirement)
    with pytest.raises(OSError, match="simulated crash"):
        ensure_unified_probation(store, job_id, now=started + timedelta(hours=1))
    assert len(load_probation(store, job_id)["trials"]) == 1
    assert (
        store.read_json(job_id, "state/evolution_experiment.json")["status"] == "active"
    )

    monkeypatch.setattr(store, "write_json", original_write)
    ensure_unified_probation(store, job_id, now=started + timedelta(hours=2))
    doc = ensure_unified_probation(store, job_id, now=started + timedelta(hours=3))

    assert len(doc["trials"]) == 1
    archived = store.read_json(job_id, "state/evolution_experiment.json")
    assert archived["status"] == "migrated"
    journal = (store.job_dir(job_id) / "journal.jsonl").read_text(encoding="utf-8")
    assert journal.count("evolution_experiment_migrated_to_probation") == 1


def test_zero_trade_burn_in_advances_to_forward_on_identical_covered_bars(
    tmp_path: Path,
) -> None:
    store, job_id = _job(tmp_path)
    candidate, revision = _candidate(store, job_id, "zero-trade")
    started = datetime(2026, 8, 1, tzinfo=UTC)
    staged = stage_evolution_probation(
        store,
        job_id,
        candidate_id="c03",
        candidate_root=candidate,
        revision=revision,
        source="evolution_campaign",
        family="compression",
        now=started,
    )
    stamps = [started + timedelta(minutes=5 * offset) for offset in range(289)]
    trial = load_probation(store, job_id)["trials"][0]
    for role in ("candidate", "reference"):
        _write_ticks(store.job_dir(job_id) / trial[role]["stream"], stamps)

    outcomes = maybe_adjudicate_probation(
        store, job_id, now=started + timedelta(hours=24)
    )

    updated = load_probation(store, job_id)["trials"][0]
    assert staged["status"] == "burn_in"
    assert updated["status"] == "active"
    assert updated["phase"] == "forward"
    assert updated["burn_in"]["coverage"] == 1.0
    assert outcomes == [
        {"action": "probation_forward_started", "trial_id": updated["trial_id"]}
    ]


def test_burn_in_uses_the_jobs_declared_bar_interval(tmp_path: Path) -> None:
    store, job_id = _job(tmp_path)
    job = store.load(job_id)
    job.execution_spec = {"data_contract": {"bar_interval": "1h"}}
    store.save(job)
    candidate, revision = _candidate(store, job_id, "hourly")
    started = datetime(2026, 8, 1, tzinfo=UTC)
    stage_evolution_probation(
        store,
        job_id,
        candidate_id="hourly-candidate",
        candidate_root=candidate,
        revision=revision,
        source="evolution_campaign",
        family="hourly-breakout",
        now=started,
    )
    trial = load_probation(store, job_id)["trials"][0]
    stamps = [started + timedelta(hours=offset) for offset in range(25)]
    for role in ("candidate", "reference"):
        _write_ticks(store.job_dir(job_id) / trial[role]["stream"], stamps)

    maybe_adjudicate_probation(store, job_id, now=started + timedelta(hours=24))

    updated = load_probation(store, job_id)["trials"][0]
    assert updated["burn_in"]["bar_interval_seconds"] == 3600
    assert updated["burn_in"]["coverage"] == 1.0
    assert updated["status"] == "active"


def test_burn_in_kills_candidate_execution_error_immediately(tmp_path: Path) -> None:
    store, job_id = _job(tmp_path)
    candidate, revision = _candidate(store, job_id, "error")
    started = datetime(2026, 8, 1, tzinfo=UTC)
    stage_evolution_probation(
        store,
        job_id,
        candidate_id="error-candidate",
        candidate_root=candidate,
        revision=revision,
        source="evolution_campaign",
        family="error-family",
        now=started,
    )
    doc = load_probation(store, job_id)
    doc["trials"][0]["candidate"]["error_count"] = 1
    store.write_json(job_id, "probation.json", doc)

    outcomes = maybe_adjudicate_probation(
        store, job_id, now=started + timedelta(minutes=5)
    )

    updated = load_probation(store, job_id)["trials"][0]
    assert updated["status"] == "killed"
    assert updated["verdict_reason"] == "candidate execution error"
    assert outcomes[0]["action"] == "probation_killed"


def test_burn_in_kills_hard_drawdown_breach_immediately(tmp_path: Path) -> None:
    store, job_id = _job(tmp_path)
    candidate, revision = _candidate(store, job_id, "breach")
    started = datetime(2026, 8, 1, tzinfo=UTC)
    stage_evolution_probation(
        store,
        job_id,
        candidate_id="breach-candidate",
        candidate_root=candidate,
        revision=revision,
        source="evolution_campaign",
        family="breach-family",
        now=started,
    )
    trial = load_probation(store, job_id)["trials"][0]
    stream = store.job_dir(job_id) / trial["candidate"]["stream"]
    stream.mkdir(parents=True, exist_ok=True)
    _write_trade(stream, started + timedelta(minutes=5), -3_000.0)

    outcomes = maybe_adjudicate_probation(
        store, job_id, now=started + timedelta(minutes=10)
    )

    updated = load_probation(store, job_id)["trials"][0]
    assert updated["status"] == "killed"
    assert updated["verdict_reason"] == "hard safety breach"
    assert updated["burn_in"]["hard_constraint_breach"] is True
    assert outcomes[0]["action"] == "probation_killed"


def test_forward_verdicts_only_peek_at_preregistered_day_7_and_14(
    tmp_path: Path,
) -> None:
    store, job_id = _job(tmp_path)
    candidate, revision = _candidate(store, job_id, "checkpoints")
    started = datetime(2026, 8, 1, tzinfo=UTC)
    stage_evolution_probation(
        store,
        job_id,
        candidate_id="checkpoint-candidate",
        candidate_root=candidate,
        revision=revision,
        source="evolution_campaign",
        family="checkpoint-family",
        now=started,
    )
    doc = load_probation(store, job_id)
    trial = doc["trials"][0]
    trial["status"] = "active"
    trial["phase"] = "forward"
    trial["burn_in"]["status"] = "passed"
    trial["forward"]["started_at"] = started.isoformat()
    trial["forward"]["deadline_at"] = (started + timedelta(days=14)).isoformat()
    for role in ("candidate", "reference"):
        trial[role]["stream"] = (
            f"results/forward/probation/{trial['trial_id']}/forward/{role}"
        )
    store.write_json(job_id, "probation.json", doc)

    for count, current in ((8, 8), (9, 9), (14, 14)):
        stamps = [started + timedelta(days=offset) for offset in range(count)]
        for role in ("candidate", "reference"):
            _write_ticks(store.job_dir(job_id) / trial[role]["stream"], stamps)
        outcomes = maybe_adjudicate_probation(
            store, job_id, now=started + timedelta(days=current)
        )
        if current == 8:
            assert [row["action"] for row in outcomes] == [
                "probation_checkpoint_inconclusive"
            ]
            assert (
                load_probation(store, job_id)["trials"][0]["forward"][
                    "last_decision_day"
                ]
                == 7
            )
        elif current == 9:
            assert outcomes == []

    updated = load_probation(store, job_id)["trials"][0]
    assert updated["status"] == "inconclusive"
    assert updated["verdict_reason"] == "14-day endpoint inconclusive"


def test_positive_paired_forward_lcb_graduates_to_owner_only_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job_id = _job(tmp_path)
    candidate, revision = _candidate(store, job_id, "positive")
    started = datetime(2026, 8, 1, tzinfo=UTC)
    stage_evolution_probation(
        store,
        job_id,
        candidate_id="positive-candidate",
        candidate_root=candidate,
        revision=revision,
        source="evolution_campaign",
        family="breakout",
        now=started,
    )
    doc = load_probation(store, job_id)
    trial = doc["trials"][0]
    trial["status"] = "active"
    trial["phase"] = "forward"
    trial["burn_in"]["status"] = "passed"
    trial["forward"]["started_at"] = started.isoformat()
    trial["forward"]["deadline_at"] = (started + timedelta(days=14)).isoformat()
    for role in ("candidate", "reference"):
        trial[role]["stream"] = (
            f"results/forward/probation/{trial['trial_id']}/forward/{role}"
        )
    store.write_json(job_id, "probation.json", doc)
    stamps = [started + timedelta(days=offset) for offset in range(8)]
    for role in ("candidate", "reference"):
        stream = store.job_dir(job_id) / trial[role]["stream"]
        _write_ticks(stream, stamps)
        if role == "candidate":
            for stamp in stamps:
                _write_trade(stream, stamp, 10.0)

    captured: dict = {}

    def fake_propose(*args, **kwargs):
        captured.update(kwargs)
        return {"proposal_id": kwargs["proposal_id"]}

    monkeypatch.setattr("wayfinder_paths.jobs.proposals.propose_change", fake_propose)

    outcomes = maybe_adjudicate_probation(
        store, job_id, now=started + timedelta(days=8)
    )

    updated = load_probation(store, job_id)["trials"][0]
    assert updated["status"] == "graduated"
    assert updated["forward"]["metrics"]["paired_days"] >= 7
    assert updated["forward"]["metrics"]["lcb"] > 0
    assert updated["promotion"]["status"] == "owner_review"
    assert captured["allow_auto_apply"] is False
    assert any(row["action"] == "probation_promotion_proposed" for row in outcomes)


def test_negative_paired_forward_ucb_kills_at_day_seven(tmp_path: Path) -> None:
    store, job_id = _job(tmp_path)
    candidate, revision = _candidate(store, job_id, "negative")
    started = datetime(2026, 8, 1, tzinfo=UTC)
    stage_evolution_probation(
        store,
        job_id,
        candidate_id="negative-candidate",
        candidate_root=candidate,
        revision=revision,
        source="evolution_campaign",
        family="negative-family",
        now=started,
    )
    doc = load_probation(store, job_id)
    trial = doc["trials"][0]
    trial["status"] = "active"
    trial["phase"] = "forward"
    trial["burn_in"]["status"] = "passed"
    trial["forward"]["started_at"] = started.isoformat()
    trial["forward"]["deadline_at"] = (started + timedelta(days=14)).isoformat()
    for role in ("candidate", "reference"):
        trial[role]["stream"] = (
            f"results/forward/probation/{trial['trial_id']}/forward/{role}"
        )
        stream = store.job_dir(job_id) / trial[role]["stream"]
        stamps = [started + timedelta(days=offset) for offset in range(8)]
        _write_ticks(stream, stamps)
        if role == "candidate":
            for stamp in stamps:
                _write_trade(stream, stamp, -10.0)
    store.write_json(job_id, "probation.json", doc)

    outcomes = maybe_adjudicate_probation(
        store, job_id, now=started + timedelta(days=8)
    )

    updated = load_probation(store, job_id)["trials"][0]
    assert updated["status"] == "killed"
    assert updated["verdict_reason"] == "paired utility UCB < 0"
    assert updated["forward"]["metrics"]["ucb"] < 0
    assert outcomes[0]["action"] == "probation_killed"


def test_probation_capacity_is_three_active_and_three_queued(tmp_path: Path) -> None:
    store, job_id = _job(tmp_path)
    statuses = []
    for index in range(7):
        candidate, revision = _candidate(store, job_id, f"capacity-{index}")
        statuses.append(
            stage_evolution_probation(
                store,
                job_id,
                candidate_id=f"candidate-{index}",
                candidate_root=candidate,
                revision=revision,
                source="evolution_campaign",
                family=f"family-{index}",
                now=datetime(2026, 8, 1, tzinfo=UTC),
            )["status"]
        )

    assert statuses == ["burn_in"] * 3 + ["queued"] * 3 + ["deferred"]


@pytest.mark.asyncio
async def test_symbol_block_removes_entries_but_preserves_reduce_only(
    tmp_path: Path,
) -> None:
    store, job_id = _job(tmp_path)
    evidence = store.job_dir(job_id) / "results/research/regime_break.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text('{"regime":"break"}\n', encoding="utf-8")
    now = datetime.now(UTC)
    os.utime(evidence, (now.timestamp(), now.timestamp()))

    block = risk_block_symbol(
        store,
        job_id,
        symbol="HYPE",
        reason="deterministic regime break",
        evidence_refs=["results/research/regime_break.json"],
        wake_id="wake-1",
        now=now,
    )
    with pytest.raises(ValueError, match="only the owner"):
        risk_unblock_symbol(store, job_id, symbol="HYPE", by="sensor")

    entry = OrderIntent(
        action="OPEN",
        venue="hyperliquid",
        symbol="HYPE",
        side="long",
        size=1.0,
    )
    exit_intent = OrderIntent(
        action="CLOSE",
        venue="hyperliquid",
        symbol="HYPE",
        side="short",
        size=1.0,
        reduce_only=False,
    )
    state = EngineState(
        pending_intents=[entry, exit_intent],
        resting_orders={
            "entry-order": RestingOrder(entry, now.isoformat()),
            "exit-order": RestingOrder(exit_intent, now.isoformat()),
        },
        mode="live",
    )

    class Broker:
        canceled: list[str] = []

        async def cancel_resting_order(self, order: RestingOrder):
            client_order_id = str(order.intent.client_order_id or "entry-order")
            self.canceled.append(client_order_id)
            return FillEvent(
                status="filled",
                venue="hyperliquid",
                symbol=order.intent.symbol,
                side=order.intent.side,
                client_order_id=client_order_id,
            )

    broker = Broker()
    events = await _apply_symbol_entry_blocks(
        state=state,
        brokers={"hyperliquid": broker},
        blocked_symbols={"HYPE"},
        now=pd.Timestamp(now),
    )

    assert block["status"] == "blocked"
    assert set(active_symbol_blocks(store, job_id)) == {"HYPE"}
    assert state.pending_intents == [exit_intent]
    assert set(state.resting_orders) == {"exit-order"}
    assert broker.canceled == ["entry-order"]
    assert {row["kind"] for row in events} == {
        "pending_entry_canceled_by_symbol_block",
        "resting_entry_canceled_by_symbol_block",
    }
    cleared = risk_unblock_symbol(
        store, job_id, symbol="HYPE", by="owner", reason="owner reviewed"
    )
    assert cleared["status"] == "cleared"


def test_unreadable_symbol_overrides_fail_closed_and_latch_once(tmp_path: Path) -> None:
    store, job_id = _job(tmp_path)
    path = store.job_dir(job_id) / "state/risk_overrides.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    first = enforced_symbol_blocks(store, job_id)
    second = enforced_symbol_blocks(store, job_id)

    assert set(first) == {"BTC", "HYPE"}
    assert second == first
    halt = read_halt(store.job_dir(job_id))
    assert halt is not None
    assert halt["source"] == "symbol_risk_override"
    with pytest.raises(PermissionError):
        clear_halt(store, job_id, by="agent")
    with pytest.raises(ValueError, match="owner repair"):
        risk_unblock_symbol(store, job_id, symbol="HYPE", by="owner")
    journal = (store.job_dir(job_id) / "journal.jsonl").read_text(encoding="utf-8")
    assert journal.count("risk_overrides_unreadable") == 1


def test_unreadable_symbol_overrides_sync_as_fail_closed(tmp_path: Path) -> None:
    store, job_id = _job(tmp_path)
    path = store.job_dir(job_id) / "state/risk_overrides.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    snapshot = snapshot_job(job_id, store=store)["risk_overrides"]

    assert snapshot["unreadable"] is True
    assert "JSONDecodeError" in snapshot["reason"]
    assert set(snapshot["symbols"]) == {"BTC", "HYPE"}
    assert all(
        block["status"] == "blocked" and block["blocked_by"] == "fail_closed"
        for block in snapshot["symbols"].values()
    )


@pytest.mark.asyncio
async def test_live_symbol_block_preserves_unconfirmed_resting_entry() -> None:
    now = pd.Timestamp("2026-08-30T12:00:00Z")
    entry = OrderIntent(
        action="OPEN",
        venue="hyperliquid",
        symbol="HYPE",
        side="long",
        size=1.0,
        client_order_id="entry-order",
    )
    state = EngineState(
        resting_orders={
            "entry-order": RestingOrder(entry, now.isoformat(), order_id="42")
        },
        mode="live",
    )

    class Broker:
        async def cancel_resting_order(self, order: RestingOrder) -> FillEvent:
            return FillEvent(
                status="ambiguous",
                venue="hyperliquid",
                symbol=order.intent.symbol,
                side=order.intent.side,
                client_order_id=order.intent.client_order_id,
                error="venue timeout",
            )

    events = await _apply_symbol_entry_blocks(
        state=state,
        brokers={"hyperliquid": Broker()},
        blocked_symbols={"HYPE"},
        now=now,
    )

    assert set(state.resting_orders) == {"entry-order"}
    assert events == [
        {
            "kind": "resting_entry_cancel_failed_by_symbol_block",
            "symbol": "HYPE",
            "client_order_id": "entry-order",
            "timestamp": now.isoformat(),
            "cancel_error": "venue timeout",
        }
    ]
