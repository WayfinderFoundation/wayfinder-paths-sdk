from __future__ import annotations

import asyncio
import json
import math
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import get_args

import pytest
import yaml

from wayfinder_paths.jobs.bench.aggregate import aggregate_experiment
from wayfinder_paths.jobs.bench.env import sandbox_relative
from wayfinder_paths.jobs.bench.forward_replay import (
    _behavior_distance,
    race_bundles,
    replay_probation,
)
from wayfinder_paths.jobs.bench.identity import (
    compare_identities,
    ensure_model_declared,
    runtime_identity,
)
from wayfinder_paths.jobs.bench.mcp_server import (
    _ALLOWED_ACTIONS as BENCH_ALLOWED_ACTIONS,
)
from wayfinder_paths.jobs.bench.mcp_server import BenchAction
from wayfinder_paths.jobs.bench.mcp_server import core_jobs as bench_core_jobs
from wayfinder_paths.jobs.bench.runner import (
    _arm_env,
    _assert_bench_root,
    _audit_session_isolation,
    _install_job,
    _resolve_runtime_opencode_config,
    _scorecard,
    _set_provider_base_url,
    _validate_config,
    run_experiment,
)
from wayfinder_paths.jobs.bench.world import load_world, prepare_world
from wayfinder_paths.jobs.benchmarks.agent_adapter import (
    install_agent_workspace,
    run_agent_wakes,
)
from wayfinder_paths.jobs.bundles import resolve_bundle_script_entrypoint
from wayfinder_paths.jobs.evolution_campaign import _campaign_now
from wayfinder_paths.jobs.execution import ExecutionSpec
from wayfinder_paths.jobs.gating import compute_workspace_revision
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.probation import stage_evolution_probation
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.worker import build_evolution_stage_prompt


def _bars(count: int = 456) -> list[dict]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        {
            "timestamp": (start + timedelta(hours=index)).isoformat(),
            "symbol": "IMX",
            "open": 10.0 + index * 0.01,
            "high": 10.1 + index * 0.01,
            "low": 9.9 + index * 0.01,
            "close": 10.0 + index * 0.01,
            "volume": 100.0,
        }
        for index in range(count)
    ]


def _declaring(*names: str):
    """An execution spec declaring ``names`` as file features, for the replay merge."""
    from wayfinder_paths.jobs.execution.primitives import ExecutionSpec

    return ExecutionSpec.from_dict(
        {
            "data_contract": {
                "bar_interval": "1h",
                "symbols": ["IMX"],
                "features": [{"name": name, "source": "file"} for name in names],
            }
        }
    )


def _job(tmp_path: Path) -> tuple[JobStore, str, list[dict]]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "majors-5m-lab",
        name="Majors lab",
        script="workspace/src/strategy.py",
        agent_mode="intervene",
        execution_contract="jobs_v1",
        interval_seconds=3600,
    )
    spec = ExecutionSpec()
    spec.data_contract.update({"bar_interval": "1h", "symbols": ["IMX"]})
    job.execution_spec = spec.to_dict()
    job.execution_params = {
        "symbols": ["IMX"],
        "initial_capital": 10_000.0,
        "warmup_bars": 20,
    }
    store.save(job)
    root = store.job_dir(job.id)
    (root / "workspace/src/strategy.py").write_text(
        "def decide(ctx):\n    return []\n", encoding="utf-8"
    )
    rows = _bars()
    (root / "results/backtest/input_bars.json").write_text(
        json.dumps({"metadata": {"days": 19}, "bars": rows}), encoding="utf-8"
    )
    return store, job.id, rows


def _world(tmp_path: Path) -> tuple[JobStore, str, Path, Path, list[dict]]:
    store, job_id, rows = _job(tmp_path / "source")
    cutoff = datetime(2026, 1, 3, 23, tzinfo=UTC)
    end = datetime(2026, 1, 19, 23, tzinfo=UTC)
    world_dir = tmp_path / "world"
    sealed_dir = tmp_path / "owner-sealed"
    prepare_world(
        store.job_dir(job_id),
        world_dir,
        generation_cutoff=cutoff,
        holdout_end=end,
        sealed_dir=sealed_dir,
        world_id="majors-losing",
    )
    return store, job_id, world_dir, sealed_dir, rows


def test_world_split_is_chronological_and_sealed(tmp_path: Path) -> None:
    _, _, world_dir, sealed_dir, _ = _world(tmp_path)

    world = load_world(world_dir, sealed_dir)

    assert world["manifest"]["development_bars"] == 72
    assert world["manifest"]["holdout_bars"] == 384
    assert "holdout" not in (world_dir / "bars.json").read_text(encoding="utf-8")
    manifest_text = (world_dir / "world.json").read_text(encoding="utf-8")
    assert str(sealed_dir) not in manifest_text
    assert not sealed_dir.is_relative_to(world_dir)
    baseline = world["manifest"]["baseline"]
    assert baseline["bars"] == 72
    assert baseline["partition"] == "development"
    assert baseline["sha256"]


def test_bundle_entrypoint_rebinds_repo_relative_live_job_path(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    expected = bundle / "workspace/src/strategy.py"

    resolved = resolve_bundle_script_entrypoint(
        bundle,
        {
            "script_loop": {
                "entrypoint": (
                    ".wayfinder/jobs/majors-5m-lab/workspace/src/strategy.py"
                )
            }
        },
    )

    assert resolved == expected


def test_world_truncates_features_and_installs_only_development_prefix(
    tmp_path: Path,
) -> None:
    store, job_id, rows = _job(tmp_path / "source")
    root = store.job_dir(job_id)
    job_yaml = yaml.safe_load((root / "job.yaml").read_text(encoding="utf-8"))
    job_yaml["execution_spec"]["data_contract"]["features"] = [
        {"name": "breadth", "path": "state/features.jsonl"}
    ]
    (root / "job.yaml").write_text(
        yaml.safe_dump(job_yaml, sort_keys=False), encoding="utf-8"
    )
    feature_path = root / "state/features.jsonl"
    feature_path.parent.mkdir(parents=True, exist_ok=True)
    feature_path.write_text(
        "".join(
            json.dumps(
                {
                    "timestamp": row["timestamp"],
                    "name": "breadth",
                    "value": index,
                    "symbol": None,
                }
            )
            + "\n"
            for index, row in enumerate(rows)
        ),
        encoding="utf-8",
    )
    world_dir = tmp_path / "world"
    sealed_dir = tmp_path / "sealed"
    prepare_world(
        root,
        world_dir,
        generation_cutoff=datetime(2026, 1, 3, 23, tzinfo=UTC),
        holdout_end=datetime(2026, 1, 19, 23, tzinfo=UTC),
        sealed_dir=sealed_dir,
    )

    manifest = load_world(world_dir, sealed_dir)["manifest"]
    assert manifest["features"][0]["rows"] == 72
    arm_store, arm_job_id = _install_job(
        tmp_path / "arm", world_dir=world_dir, policy={}
    )
    installed = arm_store.job_dir(arm_job_id) / "state/features.jsonl"
    assert len(installed.read_text(encoding="utf-8").splitlines()) == 72
    installed_bars = json.loads(
        (arm_store.job_dir(arm_job_id) / "results/backtest/input_bars.json").read_text(
            encoding="utf-8"
        )
    )
    assert len(installed_bars["bars"]) == 72
    installed_baseline = json.loads(
        (arm_store.job_dir(arm_job_id) / "results/backtest/baseline.json").read_text(
            encoding="utf-8"
        )
    )
    assert installed_baseline["scope"]["partition"] == "development"
    assert installed_baseline["scope"]["bars"] == 72


def test_race_is_deterministic_and_does_not_mutate_bundles(tmp_path: Path) -> None:
    _, _, world_dir, sealed_dir, _ = _world(tmp_path)
    a = tmp_path / "bundle-a"
    b = tmp_path / "bundle-b"
    shutil.copytree(world_dir / "incumbent", a)
    shutil.copytree(world_dir / "incumbent", b)
    a_job = yaml.safe_load((a / "job.yaml").read_text(encoding="utf-8"))
    a_job["execution_params"]["target_regimes"] = ["up_lowvol"]
    (a / "job.yaml").write_text(
        yaml.safe_dump(a_job, sort_keys=False), encoding="utf-8"
    )
    revisions = (compute_workspace_revision(a), compute_workspace_revision(b))

    output = tmp_path / "race"
    first = race_bundles(
        a,
        b,
        world_dir=world_dir,
        sealed_dir=sealed_dir,
        output_dir=output,
    )
    second = race_bundles(a, b, world_dir=world_dir, sealed_dir=sealed_dir)

    assert first["verdict"] == "invalid"
    assert first["a"]["stats"]["regime"]["target_regimes"] == ["up_lowvol"]
    assert first["paired_daily_utility_delta"] == second["paired_daily_utility_delta"]
    assert revisions == (
        compute_workspace_revision(a),
        compute_workspace_revision(b),
    )
    assert (output / "results/a/trades.json").exists()
    assert (output / "results/b/equity.json").exists()
    assert (output / "results/compare.json").exists()


def test_behavior_distance_detects_sizing_only_changes() -> None:
    base = {
        "timestamp": "2026-08-19T20:00:00+00:00",
        "symbol": "HYPE",
        "side": "sell",
    }

    distance = _behavior_distance(
        [{**base, "filled_size": 0.25}],
        [{**base, "filled_size": 1.0}],
    )

    assert distance == {
        "behavior_changed": True,
        "changed_decisions": 0,
        "changed_fill_records": 2,
        "jaccard_distance": 1.0,
    }


def test_bench_job_is_forced_to_paper_without_wallet(tmp_path: Path) -> None:
    store, job_id, _ = _job(tmp_path / "source")
    source = store.job_dir(job_id)
    job_yaml = yaml.safe_load((source / "job.yaml").read_text(encoding="utf-8"))
    job_yaml["script_loop"]["mode"] = "live"
    job_yaml["execution_params"]["wallet_label"] = "must-not-survive"
    (source / "job.yaml").write_text(
        yaml.safe_dump(job_yaml, sort_keys=False), encoding="utf-8"
    )
    world_dir = tmp_path / "world"
    sealed_dir = tmp_path / "sealed"
    prepare_world(
        source,
        world_dir,
        generation_cutoff=datetime(2026, 1, 3, 23, tzinfo=UTC),
        holdout_end=datetime(2026, 1, 19, 23, tzinfo=UTC),
        sealed_dir=sealed_dir,
    )

    arm_store, arm_job_id = _install_job(
        tmp_path / "arm", world_dir=world_dir, policy={}
    )
    installed = yaml.safe_load(
        (arm_store.job_dir(arm_job_id) / "job.yaml").read_text(encoding="utf-8")
    )

    assert installed["script_loop"]["mode"] == "paper"
    assert "wallet_label" not in installed["execution_params"]


def test_world_and_tool_isolation_fail_closed(tmp_path: Path) -> None:
    store, job_id, _ = _job(tmp_path / "source")
    world_dir = tmp_path / "world"
    with pytest.raises(ValueError, match="physically outside"):
        prepare_world(
            store.job_dir(job_id),
            world_dir,
            generation_cutoff=datetime(2026, 1, 3, 23, tzinfo=UTC),
            holdout_end=datetime(2026, 1, 19, 23, tzinfo=UTC),
            sealed_dir=world_dir / "sealed",
        )
    with pytest.raises(ValueError, match="unavailable in benchmark mode"):
        asyncio.run(
            bench_core_jobs(  # type: ignore[arg-type]
                "fetch_dataset", job_id="bench-only"
            )
        )
    with pytest.raises(ValueError, match="cannot live inside"):
        _assert_bench_root(tmp_path / ".wayfinder/jobs/a-bench")


def test_session_isolation_separates_denied_attempts_from_breaches(
    tmp_path: Path,
) -> None:
    sandbox = tmp_path / "arm"
    sandbox.mkdir()
    database = tmp_path / "opencode.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE part (session_id TEXT, data TEXT)")

    def tool_part(
        *,
        path: Path,
        status: str,
        error: str | None = None,
        tool: str = "read",
    ) -> str:
        return json.dumps(
            {
                "type": "tool",
                "tool": tool,
                "state": {
                    "status": status,
                    "input": {"filePath": str(path)},
                    "error": error,
                },
            }
        )

    denied_path = tmp_path / "denied" / "future.json"
    escaped_path = tmp_path / "escaped" / "future.json"
    connection.executemany(
        "INSERT INTO part(session_id, data) VALUES (?, ?)",
        [
            (
                "session-denied",
                tool_part(
                    path=denied_path,
                    status="error",
                    error=(
                        "The user has specified a rule which prevents you from "
                        "using this specific tool call."
                    ),
                ),
            ),
            (
                "session-breach",
                tool_part(path=escaped_path, status="completed"),
            ),
            (
                "session-invalid-url",
                tool_part(
                    path=escaped_path,
                    status="error",
                    error="URL must start with http:// or https://",
                    tool="webfetch",
                ),
            ),
        ],
    )
    connection.commit()
    connection.close()

    denied_result = _audit_session_isolation(
        ["session-denied"],
        session_db=database,
        sandbox=sandbox,
        protected_roots=[],
        missing_transcripts=[],
    )

    assert denied_result["passed"] is True
    assert denied_result["breaches"] == []
    assert denied_result["denied_attempts"] == [
        {
            "type": "denied_filesystem_escape",
            "session_id": "session-denied",
            "tool": "read",
            "path": str(denied_path),
        }
    ]

    invalid_url_result = _audit_session_isolation(
        ["session-invalid-url"],
        session_db=database,
        sandbox=sandbox,
        protected_roots=[],
        missing_transcripts=[],
    )
    assert invalid_url_result["passed"] is True
    assert invalid_url_result["breaches"] == []
    assert invalid_url_result["denied_attempts"] == [
        {
            "type": "denied_network_or_shell_tool",
            "session_id": "session-invalid-url",
            "tool": "webfetch",
        }
    ]

    result = _audit_session_isolation(
        ["session-breach"],
        session_db=database,
        sandbox=sandbox,
        protected_roots=[],
        missing_transcripts=[],
    )

    assert result["passed"] is False
    assert result["breaches"] == [
        {
            "type": "filesystem_escape",
            "session_id": "session-breach",
            "tool": "read",
            "path": str(escaped_path),
        }
    ]
    assert result["denied_attempts"] == []


def test_benchmark_design_validation_is_inline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict = {}

    async def fake_core_jobs(action: str, **kwargs: object) -> dict:
        seen.update({"action": action, **kwargs})
        return {"ok": True, "result": {}}

    monkeypatch.setattr(
        "wayfinder_paths.jobs.bench.mcp_server._production_core_jobs",
        fake_core_jobs,
    )

    asyncio.run(
        bench_core_jobs(
            "evolution_design",
            job_id="bench-only",
            campaign_design={"hypotheses": [], "slots": []},
            background=True,
        )
    )

    assert seen["background"] is False


def test_compressed_replay_uses_real_burn_in_and_day14_endpoint(
    tmp_path: Path,
) -> None:
    store, job_id, world_dir, sealed_dir, _ = _world(tmp_path)
    root = store.job_dir(job_id)
    candidate = root / "research/candidates/candidate-1"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(world_dir / "incumbent", candidate)
    (candidate / "workspace/src/strategy.py").write_text(
        "# candidate\ndef decide(ctx):\n    return []\n", encoding="utf-8"
    )
    cutoff = datetime.fromisoformat(
        load_world(world_dir, sealed_dir)["manifest"]["generation_cutoff"]
    )
    stage_evolution_probation(
        store,
        job_id,
        candidate_id="candidate-1",
        candidate_root=candidate,
        revision=compute_workspace_revision(candidate),
        source="evolution_campaign",
        family="test-family",
        now=cutoff,
    )
    world = load_world(world_dir, sealed_dir)

    result = replay_probation(
        store,
        job_id,
        development_rows=world["development"]["bars"],
        holdout_rows=world["holdout"]["bars"],
        generation_cutoff=cutoff,
    )

    assert result["burn_in"]["status"] == "passed"
    assert result["status"] == "inconclusive"
    assert result["forward"]["max_paired_days"] == 14
    assert (root / "results/forward/probation").exists()


def test_campaign_stage_is_retried_once_before_the_cell_is_invalidated(
    tmp_path, monkeypatch
) -> None:
    import wayfinder_paths.jobs.bench.runner as runner_module

    statuses = iter(
        [{"status": "active"}, {"status": "active"}, {"status": "complete"}]
    )
    calls: list[dict] = []

    def fake_status(store, job_id):
        return next(statuses)

    def fake_prompt(store, job_id, *, now):
        return {"campaign_id": "campaign-1", "session_stage": "candidate-01"}

    def fake_render(job_id, block, *, prior_handoff=None):
        return {
            "prompt": "do the stage",
            "session_stage": "candidate-01",
            "artifact_key": "candidate-01-attempt-01",
            "title": "job/demo/evolution/campaign-1/candidate-01-attempt-01",
            "agent_name": "wayfinder-evolution-worker",
        }

    def fake_agent(**kwargs):
        calls.append(kwargs)
        # First turn dies on a bad tool call; the retry runs clean.
        exit_code = 1 if len(calls) == 1 else 0
        return {"exit_code": exit_code, "stdout_tail": "", "stderr_tail": ""}

    monkeypatch.setattr(runner_module, "campaign_status", fake_status)
    monkeypatch.setattr(runner_module, "campaign_prompt_block", fake_prompt)
    monkeypatch.setattr(runner_module, "sandbox_relative", lambda value, *, root: value)
    monkeypatch.setattr(runner_module, "build_evolution_stage_prompt", fake_render)
    monkeypatch.setattr(runner_module, "run_agent_prompt", fake_agent)
    monkeypatch.setattr(runner_module, "_session_db", lambda sandbox: tmp_path / "x")
    monkeypatch.setattr(runner_module, "_lookup_session_id", lambda db, title: None)
    monkeypatch.setattr(runner_module, "_wait_for_settle", lambda *a, **k: True)

    sessions: list[dict] = []
    outcome = runner_module._drive_campaign(
        store=None,
        job_id="demo",
        model="m",
        variant=None,
        opencode=tmp_path / "opencode",
        sandbox=tmp_path,
        env={},
        virtual_now=datetime(2026, 8, 10, tzinfo=UTC),
        repeat_seed=101,
        max_turns=5,
        timeout_s=1,
        settle_timeout_s=1,
        prompt_hashes=[],
        sessions=sessions,
        stage_sessions={},
    )

    assert outcome is None
    assert [row["exit_code"] for row in sessions] == [1, 0]
    assert sessions[0]["retried"] is True
    assert calls[1]["session_id"] is None


def test_validated_signal_usage_counts_hypotheses_that_cite_the_pack() -> None:
    from wayfinder_paths.jobs.bench.runner import _validated_signal_usage

    class Store:
        def __init__(self, docs):
            self.docs = docs

        def read_json(self, job_id, relative, default=None):
            return self.docs.get(relative, default)

    store = Store(
        {
            "pack.json": {
                "validated_signals": {"available": True, "signals": [1, 2, 3]}
            },
            "design.json": {
                "hypotheses": [
                    {"evidence_refs": ["/validated_signals/signals/1/signal"]},
                    {"evidence_refs": ["/baseline/reason"]},
                ]
            },
        }
    )
    usage = _validated_signal_usage(
        store,
        "demo",
        {"diagnostic_pack": "pack.json", "campaign_design": "design.json"},
    )
    assert usage == {
        "enabled": True,
        "available": True,
        "offered": 3,
        "replicated": 0,
        "hypotheses_citing": 1,
        "composition_rounds": 0,
        "composition_proposals": 0,
        "composition_survivors": 0,
    }
    assert _validated_signal_usage(Store({}), "demo", {})["enabled"] is False


def test_campaign_prompt_formatter_is_the_production_formatter() -> None:
    campaign = {
        "campaign_id": "campaign-1",
        "session_stage": "design",
        "agent_name": "wayfinder-evolution-designer",
        "next_action": "submit the design",
        "candidate_outcomes": [{"candidate_id": "c01"}],
    }

    rendered = build_evolution_stage_prompt("majors-5m-lab", campaign)

    assert rendered["agent_name"] == "wayfinder-evolution-designer"
    assert rendered["artifact_key"] == "design"
    assert rendered["work_order"]["lane"] == "evolution_design"
    assert "submit the design" in rendered["prompt"]
    assert '"candidate_id": "c01"' in rendered["prompt"]


def test_campaign_repair_prompt_is_a_compact_artifact_work_order() -> None:
    campaign = {
        "campaign_id": "campaign-1",
        "session_stage": "candidate-01",
        "artifact_key": "candidate-01-attempt-02",
        "next_action": "repair and evaluate candidate one",
        "candidate_id": "c01",
        "postmortem_path": ".wayfinder/jobs/demo/attempts/c01/a01/postmortem.json",
        "work_inputs": [".wayfinder/jobs/demo/candidates/c01/candidate.json"],
        "editable_paths": [".wayfinder/jobs/demo/candidates/c01"],
        "candidate_outcomes": [{"candidate_id": "c01", "attempt_count": 1}],
        "cases": [{"blob": "must-not-be-repeated"}],
        "repair_work_order": {
            "primary_failure": "cost_bleed",
            "diagnosis": "59.0 fills/day vs incumbent 2.7; cadence is the defect.",
            "admissible_repairs": ["raise the minimum hold"],
            "forbidden": ["signal tweaks"],
            "budget": {"max_fills_per_day": 8.1},
        },
    }

    rendered = build_evolution_stage_prompt("majors-5m-lab", campaign)

    assert rendered["artifact_key"] == "candidate-01-attempt-02"
    assert rendered["work_order"]["lane"] == "evolution_repair"
    assert rendered["work_order"]["editable_paths"] == [
        ".wayfinder/jobs/demo/candidates/c01"
    ]
    assert "postmortem.json" in rendered["prompt"]
    assert "must-not-be-repeated" not in rendered["prompt"]
    prompt = rendered["prompt"]
    assert "Repair work order (deterministic, authoritative)" in prompt
    assert "cadence is the defect" in prompt
    assert prompt.index("Repair work order") < prompt.index(
        "Persisted candidate outcomes"
    )


def test_identity_allows_only_pre_registered_model_difference() -> None:
    base = {
        "sdk_ref": "abc",
        "model": "pro",
        "variant": None,
        "agent_hashes": {"worker": "same"},
        "data": {"sha": "same"},
        "initial_prompt_sha256": "same-prompt",
    }
    other = {**base, "model": "flash", "variant": "max"}

    assert compare_identities(base, other, allowed={"model", "variant"})["comparable"]
    other["data"] = {"sha": "different"}
    assert not compare_identities(base, other, allowed={"model", "variant"})[
        "comparable"
    ]

    preview = {
        **base,
        "arm_parameters": {"campaign": {"behavior_preview_enabled": True}},
    }
    control = {
        **base,
        "arm_parameters": {"campaign": {"behavior_preview_enabled": False}},
    }
    assert compare_identities(control, preview, allowed={"arm_parameters"})[
        "comparable"
    ]
    assert not compare_identities(control, preview, allowed=set())["comparable"]
    other["data"] = base["data"]
    other["initial_prompt_sha256"] = "different-prompt"
    assert not compare_identities(base, other, allowed={"model", "variant"})[
        "comparable"
    ]


def test_runtime_identity_uses_the_frozen_sdk_ref(tmp_path: Path) -> None:
    agents = tmp_path / ".opencode" / "agents"
    agents.mkdir(parents=True)
    (agents / "worker.md").write_text("---\ntemperature: 0.1\n---\n", encoding="utf-8")
    (tmp_path / ".opencode" / "opencode.json").write_text("{}\n", encoding="utf-8")

    identity = runtime_identity(
        sdk_ref="registered-before-run",
        sandbox=tmp_path,
        model="wayfinder/model",
        variant=None,
        repeat_seed=101,
        world_manifest={
            "world_id": "world",
            "dataset": {},
            "source_revision": "source",
        },
        prompt_hashes=[],
        declared_differences=["model"],
        arm_parameters={},
        opencode=Path("/usr/bin/true"),
    )

    assert identity["sdk_ref"] == "registered-before-run"


def test_flash_max_is_declared_as_model_plus_variant(tmp_path: Path) -> None:
    config = tmp_path / "opencode.json"
    config.write_text(
        json.dumps(
            {
                "provider": {
                    "wayfinder": {
                        "models": {
                            "deepseek-v4-pro": {
                                "name": "DeepSeek V4 Pro",
                                "limit": {"context": 128000, "output": 28000},
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    assert ensure_model_declared(config, "wayfinder/deepseek-v4-flash") is True
    assert ensure_model_declared(config, "wayfinder/deepseek-v4-flash") is False
    loaded = json.loads(config.read_text(encoding="utf-8"))
    assert "deepseek-v4-flash" in loaded["provider"]["wayfinder"]["models"]


def test_shell_runtime_config_is_copied_and_dev_endpoint_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "sdk"
    (repo / ".opencode/agents").mkdir(parents=True)
    relative_rule = '    ".wayfinder/jobs/**": allow\n'
    (repo / ".opencode/agents/wayfinder-evolution-designer.md").write_text(
        "read:\n" + relative_rule,
        encoding="utf-8",
    )
    (repo / ".opencode/agents/wayfinder-evolution-worker.md").write_text(
        "read:\n"
        + relative_rule
        + "write:\n"
        + relative_rule
        + "edit:\n"
        + relative_rule,
        encoding="utf-8",
    )
    (repo / ".opencode/opencode.json").write_text(
        json.dumps({"provider": {"wayfinder": {"models": {}}}}), encoding="utf-8"
    )
    (repo / "wayfinder_paths").mkdir()
    runtime = tmp_path / "shell-opencode.json"
    runtime.write_text(
        json.dumps(
            {
                "provider": {
                    "wayfinder": {
                        "options": {"baseURL": "https://llm.wayfinder.ai/v1"},
                        "models": {
                            "deepseek-v4-pro": {},
                            "deepseek-v4-flash": {},
                        },
                    }
                },
                "mcp": {"wayfinder": {"enabled": False}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BENCH_RUNTIME_CONFIG", str(runtime))
    resolved = _resolve_runtime_opencode_config(
        {
            "_config_dir": str(tmp_path),
            "runtime_opencode_config": "{env:BENCH_RUNTIME_CONFIG}",
        }
    )
    sandbox = tmp_path / "sandbox"

    install_agent_workspace(
        sandbox=sandbox,
        repo_root=repo,
        runtime_config=resolved,
        mcp_url="http://127.0.0.1:4321/mcp",
    )
    installed = sandbox / ".opencode/opencode.json"
    _set_provider_base_url(
        installed,
        provider="wayfinder",
        base_url="https://llm-dev.wayfinder.ai/v1/",
    )
    loaded = json.loads(installed.read_text(encoding="utf-8"))

    assert set(loaded["provider"]["wayfinder"]["models"]) == {
        "deepseek-v4-pro",
        "deepseek-v4-flash",
    }
    assert (
        loaded["provider"]["wayfinder"]["options"]["baseURL"]
        == "https://llm-dev.wayfinder.ai/v1"
    )
    assert loaded["mcp"]["wayfinder"]["url"] == "http://127.0.0.1:4321/mcp"
    normalized_rule = '    "**/.wayfinder/jobs/**": allow\n'
    installed_agents = sandbox / ".opencode/agents"
    assert (installed_agents / "wayfinder-evolution-designer.md").read_text().count(
        normalized_rule
    ) == 1
    assert (installed_agents / "wayfinder-evolution-worker.md").read_text().count(
        normalized_rule
    ) == 3
    project_root = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=sandbox,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert Path(project_root).resolve() == sandbox.resolve()


def test_arm_environment_isolates_user_home_and_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    virtual_now = datetime(2026, 8, 10, 15, 25, tzinfo=UTC)
    env = _arm_env(tmp_path / "arm", virtual_now=virtual_now)

    assert env["HOME"] == str(tmp_path / "arm/.bench-home")
    assert Path(env["HOME"]).is_dir()
    assert env["WAYFINDER_BENCHMARK_NOW"] == virtual_now.isoformat()
    monkeypatch.setenv("WAYFINDER_BENCHMARK", "1")
    monkeypatch.setenv("WAYFINDER_BENCHMARK_NOW", env["WAYFINDER_BENCHMARK_NOW"])
    assert _campaign_now() == virtual_now


def test_sandbox_paths_are_rendered_relative_for_production_permissions(
    tmp_path: Path,
) -> None:
    root = tmp_path / "arm"
    absolute = root / ".wayfinder/jobs/bench/research/diagnostic_pack.json"

    rendered = sandbox_relative(
        {"next_action": f"Read `{absolute}`", "paths": [str(absolute)]}, root=root
    )

    assert rendered == {
        "next_action": "Read `.wayfinder/jobs/bench/research/diagnostic_pack.json`",
        "paths": [".wayfinder/jobs/bench/research/diagnostic_pack.json"],
    }


def test_budget_parity_is_explicit_and_four_seeds_are_required_by_config() -> None:
    config = {
        "schema_version": "1.0",
        "arms": [{"name": "a"}, {"name": "b"}],
        "worlds": [{"world_dir": "w", "sealed_dir": "s"}],
        "seeds": [1, 2, 3, 4],
    }
    _validate_config(config)
    rows = [
        {
            "arm": arm,
            "world_id": "world",
            "seed": seed,
            "holdout": {
                "paired_daily_utility_delta": {"estimate": 0.02 if arm == "a" else 0.0}
            },
            "cost": {"tokens_in": 100, "tokens_out": 10},
            "funnel": {},
            "forward": {},
            "isolation": {
                "denied_attempts": ([{"type": "denied"}] if arm == "a" else [])
            },
        }
        for seed in config["seeds"]
        for arm in ("a", "b")
    ]

    report = aggregate_experiment(rows, arm_order=["a", "b"])

    assert report["primary"]["pairs"] == 4
    assert report["cost_parity"]["matched"] is True
    assert report["decision"] == "a_wins"
    assert report["by_arm"]["a"]["process"]["policy_denied_attempts"] == 4
    assert report["by_arm"]["b"]["process"]["policy_denied_attempts"] == 0


def test_cost_parity_basis_can_be_attempts_instead_of_tokens(
    tmp_path: Path,
) -> None:
    rows = [
        {
            "arm": arm,
            "world_id": "world",
            "seed": seed,
            "holdout": {
                "paired_daily_utility_delta": {"estimate": 0.02 if arm == "a" else 0.0}
            },
            "cost": {"tokens_in": 150 if arm == "a" else 100, "tokens_out": 0},
            "funnel": {"attempts": 22},
            "forward": {},
        }
        for seed in (1, 2, 3, 4)
        for arm in ("a", "b")
    ]

    by_tokens = aggregate_experiment(rows, arm_order=["a", "b"])
    by_attempts = aggregate_experiment(
        rows, arm_order=["a", "b"], cost_parity_basis="attempts", output_dir=tmp_path
    )

    assert by_tokens["cost_parity"] == {
        "basis": "tokens",
        "ratio": 1.5,
        "matched": False,
    }
    assert by_tokens["decision"] == "invalid_cost_mismatch"
    assert by_attempts["cost_parity"] == {
        "basis": "attempts",
        "ratio": 1.0,
        "matched": True,
    }
    assert by_attempts["decision"] == "a_wins"
    assert by_attempts["pre_registered_rule"]["cost_parity_basis"] == "attempts"
    assert "attempts cost ratio" in by_attempts["pre_registered_rule"]["decision"]
    assert "Cost parity (attempts): ratio=1.0 matched=True" in (
        tmp_path / "report.txt"
    ).read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="cost_parity_basis"):
        aggregate_experiment(rows, arm_order=["a", "b"], cost_parity_basis="wall")


def test_single_seed_pilot_is_directional_only() -> None:
    config = {
        "schema_version": "1.0",
        "pilot": True,
        "arms": [{"name": "a"}, {"name": "b"}],
        "worlds": [{"world_dir": "w", "sealed_dir": "s"}],
        "seeds": [1],
    }
    _validate_config(config)
    rows = [
        {
            "arm": arm,
            "world_id": "world",
            "seed": 1,
            "holdout": {
                "paired_daily_utility_delta": {"estimate": 0.02 if arm == "a" else 0.0}
            },
            "cost": {"tokens_in": 100, "tokens_out": 10},
            "funnel": {},
            "forward": {},
        }
        for arm in ("a", "b")
    ]

    report = aggregate_experiment(rows, arm_order=["a", "b"], pilot=True)

    assert report["primary"]["pairs"] == 1
    assert report["pilot"] is True
    assert report["decision"] == "pilot_directional_only"


def test_behavior_preview_is_an_allowed_pre_registered_arm_parameter() -> None:
    config = {
        "schema_version": "1.0",
        "pilot": True,
        "arms": [
            {
                "name": "control",
                "campaign": {"behavior_preview_enabled": False},
            },
            {
                "name": "preview",
                "campaign": {"behavior_preview_enabled": True},
            },
        ],
        "allowed_identity_differences": ["arm_parameters"],
        "worlds": [{"world_dir": "w", "sealed_dir": "s"}],
        "seeds": [1],
    }

    _validate_config(config)

    config["max_parallel_arms"] = 4
    _validate_config(config)
    config["max_parallel_arms"] = 9
    with pytest.raises(ValueError, match="max_parallel_arms"):
        _validate_config(config)


def test_scorecard_counts_preview_ticks_and_avoided_quick_simulation() -> None:
    scorecard = _scorecard(
        state={
            "counts": {},
            "candidates": [
                {
                    "attempts": [
                        {
                            "postmortem": {"behavior_diff": {"material_change": False}},
                            "outcome": {
                                "quick_simulation_ran": False,
                                "behavior_preview": {
                                    "status": "unchanged",
                                    "ticks_evaluated": 24,
                                },
                            },
                        }
                    ]
                }
            ],
        },
        funnel={},
        forward={},
        holdout={},
        sessions=[],
        cost={},
    )

    assert scorecard["process"]["behavior_unchanged_attempts"] == 1
    assert scorecard["process"]["quick_simulations"] == 0
    assert scorecard["process"]["behavior_preview_rejections"] == 1
    assert scorecard["process"]["behavior_preview_ticks"] == 24


def test_experiment_runs_isolated_arms_concurrently_and_preserves_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import wayfinder_paths.jobs.bench.runner as runner_module

    output = tmp_path / "experiment"
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "pilot": True,
                "output_dir": str(output),
                "arms": [{"name": "a"}, {"name": "b"}],
                "worlds": [{"world_dir": "world", "sealed_dir": "sealed"}],
                "seeds": [1, 2],
                "max_parallel_arms": 2,
            }
        ),
        encoding="utf-8",
    )
    guard = threading.Lock()
    active = 0
    peak = 0

    def fake_arm(*, arm, seed, **kwargs):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03 if arm["name"] == "a" else 0.01)
        with guard:
            active -= 1
        name = str(arm["name"])
        return {
            "run_id": f"world-{seed}-{name}",
            "world_id": "world",
            "seed": seed,
            "arm": name,
            "identity": {"same": True},
            "invalid_reason": None,
            "holdout": {
                "paired_daily_utility_delta": {"estimate": 0.01 if name == "a" else 0}
            },
            "cost": {"tokens_in": 10, "tokens_out": 1},
        }

    monkeypatch.setattr(runner_module, "_runtime_pins", lambda *args: {})
    monkeypatch.setattr(runner_module, "_run_arm_in_declared_sdk", fake_arm)

    result = run_experiment(config_path)

    assert peak == 2
    assert result["pilot"] is True
    assert result["primary"]["pairs"] == 2
    assert sorted(path.name for path in (output / "runs").glob("*.json")) == [
        "world-1-a.json",
        "world-1-b.json",
        "world-2-a.json",
        "world-2-b.json",
    ]


def test_bench_mcp_exposes_read_only_research_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert {
        "status",
        "report",
        "regime_health",
        "attribution",
        "signal_check",
        "signal_scan",
        "backtest_diagnose",
        "holdout_check",
        "evolution_compose",
        "evolution_mechanism_grid",
    } <= BENCH_ALLOWED_ACTIONS
    assert BENCH_ALLOWED_ACTIONS.isdisjoint(
        {"propose", "fetch_dataset", "chart", "analogs", "evolution_start"}
    )
    assert set(get_args(BenchAction)) == BENCH_ALLOWED_ACTIONS
    seen: dict = {}

    async def fake_core_jobs(action: str, **kwargs: object) -> dict:
        seen.update({"action": action, **kwargs})
        return {"ok": True, "result": {}}

    monkeypatch.setattr(
        "wayfinder_paths.jobs.bench.mcp_server._production_core_jobs",
        fake_core_jobs,
    )
    asyncio.run(
        bench_core_jobs(
            "signal_scan", job_id="bench-only", timeframes=["1h"], window_days=30
        )
    )
    assert seen["action"] == "signal_scan"
    assert seen["timeframes"] == ["1h"]
    assert seen["window_days"] == 30
    # Composition runs inline like design: the validator's problems must
    # reach the same model turn.
    proposals = [{"name": "ws_x", "expression": "close(f) > 0", "min_bars": 2}]
    asyncio.run(
        bench_core_jobs(
            "evolution_compose",
            job_id="bench-only",
            signal_proposals=proposals,
            background=True,
        )
    )
    assert seen["action"] == "evolution_compose"
    assert seen["signal_proposals"] == proposals
    assert seen["background"] is False
    asyncio.run(
        bench_core_jobs(
            "evolution_mechanism_grid",
            job_id="bench-only",
            signal_ref="/validated_signals/replicated/0",
            side="long",
            background=True,
        )
    )
    assert seen["action"] == "evolution_mechanism_grid"
    assert seen["signal_ref"] == "/validated_signals/replicated/0"
    assert seen["side"] == "long" and seen["background"] is False


def test_bench_sandbox_restricts_job_worker_bash_and_task(tmp_path: Path) -> None:
    source_agent = (
        Path(__file__).resolve().parents[2] / ".opencode/agents/wayfinder-job-worker.md"
    )
    repo = tmp_path / "sdk"
    (repo / ".opencode/agents").mkdir(parents=True)
    shutil.copy(source_agent, repo / ".opencode/agents/wayfinder-job-worker.md")
    (repo / ".opencode/opencode.json").write_text(
        json.dumps({"mcp": {}}), encoding="utf-8"
    )
    (repo / "wayfinder_paths").mkdir()
    sandbox = tmp_path / "sandbox"

    install_agent_workspace(sandbox=sandbox, repo_root=repo, disable_mcp=True)

    def permission_block(path: Path) -> dict:
        return yaml.safe_load(path.read_text(encoding="utf-8").split("---\n")[1])[
            "permission"
        ]

    installed = permission_block(sandbox / ".opencode/agents/wayfinder-job-worker.md")
    source = permission_block(source_agent)
    assert installed["bash"] == {"*": "deny"}
    assert installed["task"] == {"*": "deny"}
    assert installed["edit"]["**/.wayfinder/jobs/**"] == "allow"
    # Last-match-wins: the catch-all deny first, the job-tree allows after it,
    # the governance plane last; the runs tree is denied, never asked.
    assert list(installed["edit"]) == [
        "*",
        ".wayfinder_runs/**",
        ".wayfinder/jobs/**",
        "**/.wayfinder/jobs/**",
        "governance/**",
        "audit/**",
    ]
    assert installed["edit"][".wayfinder_runs/**"] == "deny"
    # Every other permission line (and the load-bearing ordering of the
    # wayfinder_* entries) survives untouched.
    assert list(installed) == list(source)
    assert {
        k: v for k, v in installed.items() if k not in {"bash", "task", "edit"}
    } == {k: v for k, v in source.items() if k not in {"bash", "task", "edit"}}
    assert source_agent.read_text(encoding="utf-8") == (
        repo / ".opencode/agents/wayfinder-job-worker.md"
    ).read_text(encoding="utf-8")


def test_run_agent_wakes_forwards_variant_env_and_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []

    def fake_prepare(*, store, job_id, mode):  # noqa: ANN001
        return {"prompt": f"{job_id}:{mode}"}

    def fake_prompt(**kwargs):  # noqa: ANN003
        calls.append(kwargs)
        return {"exit_code": 0}

    monkeypatch.setattr(
        "wayfinder_paths.jobs.worker.prepare_job_worker_prompt", fake_prepare
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.benchmarks.agent_adapter.run_agent_prompt", fake_prompt
    )
    env = {"HOME": str(tmp_path)}

    sessions = run_agent_wakes(
        sandbox=tmp_path,
        job_id="demo",
        wakes=2,
        model="m",
        variant="max",
        env=env,
        title="recurrence/gen-1",
    )
    run_agent_wakes(sandbox=tmp_path, job_id="demo", wakes=1, model="m", title="solo")
    run_agent_wakes(sandbox=tmp_path, job_id="demo", wakes=1, model="m")

    assert [row["wake"] for row in sessions] == [0, 1]
    assert [call["title"] for call in calls] == [
        "recurrence/gen-1-0",
        "recurrence/gen-1-1",
        "solo",
        "wob-demo-wake-0",
    ]
    assert [(call["variant"], call["env"]) for call in calls[:2]] == [("max", env)] * 2
    assert calls[2]["variant"] is None and calls[2]["env"] is None
    assert calls[0]["prompt"] == "demo:intervene"


def test_paired_utility_deltas_scale_with_the_books_capital() -> None:
    from wayfinder_paths.jobs.bench.forward_replay import (
        environment_capital,
        paired_daily_utility_deltas,
    )

    a = {"2026-01-01": 1.0, "2026-01-02": -1.0}
    b = {"2026-01-01": 0.0, "2026-01-02": 0.0}
    big = paired_daily_utility_deltas(a, b)
    small = paired_daily_utility_deltas(a, b, capital=100.0)
    assert big[0] == pytest.approx(math.log1p(1 / 10_000))
    assert small[0] == pytest.approx(math.log1p(0.01))
    assert small[0] / big[0] > 90
    assert environment_capital({"params": {"initial_capital": 100.0}}) == 100.0
    assert environment_capital({}) == 10_000.0


@pytest.mark.parametrize(
    "agent", ["wayfinder-job-worker.md", "wayfinder-job-auto-worker.md"]
)
def test_worker_personas_put_catch_all_denies_before_their_allows(agent: str) -> None:
    """OpenCode resolves the LAST matching permission rule. A catch-all deny
    that trails an allow silently wins: the job worker ran benchmark wakes
    with no write path and no MCP tool until this ordering was fixed."""
    path = Path(__file__).resolve().parents[2] / ".opencode/agents" / agent
    permission = yaml.safe_load(path.read_text(encoding="utf-8").split("---\n")[1])[
        "permission"
    ]
    edit_keys = list(permission["edit"])
    assert edit_keys.index("*") < edit_keys.index(".wayfinder/jobs/**")
    assert permission["edit"][".wayfinder/jobs/**"] == "allow"
    mcp_keys = [key for key in permission if key.startswith("wayfinder_")]
    assert mcp_keys.index("wayfinder_*") < mcp_keys.index("wayfinder_core_jobs")
    assert permission["wayfinder_core_jobs"] == "allow"


def test_settle_recovers_a_dead_finalize_in_the_foreground(monkeypatch, tmp_path):
    from wayfinder_paths.jobs.bench import runner as runner_module

    states = [
        {"status": "finalizing", "candidates": []},
        {"status": "complete", "candidates": []},
    ]
    finalized: list[str] = []

    def fake_status(store, job_id):
        return dict(states[min(len(finalized), 1)])

    class FakeStore:
        def job_dir(self, job_id):
            return tmp_path

    monkeypatch.setattr(runner_module, "campaign_status", fake_status)
    monkeypatch.setattr(
        runner_module, "op_status_summary", lambda job_dir, op: {"status": "failed"}
    )
    monkeypatch.setattr(
        runner_module,
        "finalize_campaign",
        lambda store, job_id: finalized.append(job_id) or {},
    )
    monkeypatch.setattr(runner_module.time, "sleep", lambda seconds: None)

    assert runner_module._wait_for_settle(FakeStore(), "demo", timeout_s=30) is True
    assert finalized == ["demo"]

    def failing_finalize(store, job_id):
        raise RuntimeError("evolution completion compute duty exhausted")

    finalized.clear()
    monkeypatch.setattr(runner_module, "finalize_campaign", failing_finalize)
    with pytest.raises(runner_module.CampaignSettleError, match="duty exhausted"):
        runner_module._wait_for_settle(FakeStore(), "demo", timeout_s=30)

    # A live op is left alone: the bench only stands in for the watchdog.
    monkeypatch.setattr(
        runner_module, "op_status_summary", lambda job_dir, op: {"status": "running"}
    )
    monkeypatch.setattr(
        runner_module, "finalize_campaign", lambda store, job_id: finalized.append("x")
    )
    assert runner_module._wait_for_settle(FakeStore(), "demo", timeout_s=1) is False
    assert finalized == []


def test_world_freezes_the_macro_feature_and_reveals_the_holdout_rows(
    tmp_path: Path,
) -> None:
    from wayfinder_paths.jobs.bench.forward_replay import merge_store_features
    from wayfinder_paths.jobs.bench.world import (
        install_development_world,
        reveal_holdout_features,
    )
    from wayfinder_paths.jobs.execution.primitives import CompletedBarsView

    store, job_id, _ = _job(tmp_path / "source")
    rows = _bars(count=24 * 40)  # forty days of hourly bars
    (store.job_dir(job_id) / "results/backtest/input_bars.json").write_text(
        json.dumps({"metadata": {"days": 40}, "bars": rows}), encoding="utf-8"
    )
    cutoff = datetime(2026, 1, 25, 23, tzinfo=UTC)
    end = datetime(2026, 2, 9, 23, tzinfo=UTC)
    world_dir, sealed_dir = tmp_path / "world", tmp_path / "owner-sealed"
    manifest = prepare_world(
        store.job_dir(job_id),
        world_dir,
        generation_cutoff=cutoff,
        holdout_end=end,
        sealed_dir=sealed_dir,
        world_id="macro-world",
    )

    frozen = manifest["features"]
    assert frozen and frozen[0]["target_path"] == "state/features.jsonl"
    assert "macro_ret_7d" in frozen[0]["derived"]
    dev_rows = [
        json.loads(line)
        for line in (world_dir / frozen[0]["path"]).read_text().splitlines()
    ]
    assert dev_rows and max(r["timestamp"] for r in dev_rows) <= cutoff.isoformat()
    # The 28-day label only exists past day 28, which is inside the holdout.
    assert not any(r["name"] == "macro_regime" for r in dev_rows)
    sealed = [
        json.loads(line)
        for line in (sealed_dir / "features-holdout.jsonl").read_text().splitlines()
    ]
    assert any(r["name"] == "macro_regime" for r in sealed)
    assert min(r["timestamp"] for r in sealed) > cutoff.isoformat()
    assert manifest["holdout_features"]["holdout_rows"] == len(sealed)

    # Install: the prefix lands in the sandbox job's store; reveal appends the
    # holdout rows once, and a replay view then carries the columns.
    sandbox_job = tmp_path / "sandbox-job"
    sandbox_job.mkdir()
    install_development_world(world_dir, destination_job=sandbox_job)
    store_path = sandbox_job / "state" / "features.jsonl"
    assert len(store_path.read_text().splitlines()) == len(dev_rows)
    assert reveal_holdout_features(sealed_dir, sandbox_job) == len(sealed)
    assert reveal_holdout_features(sealed_dir, sandbox_job) == 0
    view = merge_store_features(
        CompletedBarsView.from_rows(rows),
        sandbox_job,
        spec=_declaring("macro_regime", "macro_ret_7d"),
    )
    assert view.feature("macro_regime") in {-1.0, 0.0, 1.0}
    assert view.feature("macro_ret_7d") is not None


def _leader_file(source_job: Path, *, days: int, daily_growth: float = 0.02) -> None:
    from wayfinder_paths.jobs.bench.leaders import LEADER_CLOSES_RELATIVE

    start = datetime(2026, 1, 1, tzinfo=UTC)
    closes = []
    for hour in range(24 * days):
        level = 100.0 * (1 + daily_growth) ** (hour / 24)
        stamp = (start + timedelta(hours=hour)).isoformat()
        closes.append({"timestamp": stamp, "symbol": "BTC", "close": level})
        closes.append({"timestamp": stamp, "symbol": "ETH", "close": level * 0.05})
    path = source_job / LEADER_CLOSES_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "metadata": {"interval": "1h", "symbols": ["BTC", "ETH"]},
                "closes": closes,
            }
        ),
        encoding="utf-8",
    )


def test_world_freezes_the_leader_feature_from_the_leader_file(tmp_path: Path) -> None:
    from wayfinder_paths.jobs.bench.forward_replay import merge_store_features
    from wayfinder_paths.jobs.bench.world import (
        install_development_world,
        reveal_holdout_features,
    )
    from wayfinder_paths.jobs.execution.primitives import CompletedBarsView

    store, job_id, _ = _job(tmp_path / "source")
    rows = _bars(count=24 * 40)
    (store.job_dir(job_id) / "results/backtest/input_bars.json").write_text(
        json.dumps({"metadata": {"days": 40}, "bars": rows}), encoding="utf-8"
    )
    _leader_file(store.job_dir(job_id), days=40)
    cutoff = datetime(2026, 1, 25, 23, tzinfo=UTC)
    end = datetime(2026, 2, 9, 23, tzinfo=UTC)
    world_dir, sealed_dir = tmp_path / "world", tmp_path / "owner-sealed"

    manifest = prepare_world(
        store.job_dir(job_id),
        world_dir,
        generation_cutoff=cutoff,
        holdout_end=end,
        sealed_dir=sealed_dir,
        world_id="leader-world",
    )

    frozen = manifest["features"]
    assert "leader_state" in frozen[0]["derived"]
    assert "macro_ret_7d" in frozen[0]["derived"]
    leaders = manifest["holdout_features"]["leaders"]
    assert leaders["symbols"] == ["BTC", "ETH"] and leaders["interval"] == "1h"
    dev_rows = [
        json.loads(line)
        for line in (world_dir / frozen[0]["path"]).read_text().splitlines()
    ]
    states = [r for r in dev_rows if r["name"] == "leader_state"]
    assert states and max(r["timestamp"] for r in states) <= cutoff.isoformat()
    assert {r["value"] for r in states} == {1.0}  # +2%/day is a broad rally
    sealed = [
        json.loads(line)
        for line in (sealed_dir / "features-holdout.jsonl").read_text().splitlines()
    ]
    assert any(r["name"] == "leader_state" for r in sealed)
    assert min(r["timestamp"] for r in sealed) > cutoff.isoformat()

    sandbox_job = tmp_path / "sandbox-job"
    sandbox_job.mkdir()
    install_development_world(world_dir, destination_job=sandbox_job)
    assert reveal_holdout_features(sealed_dir, sandbox_job) == len(sealed)
    assert reveal_holdout_features(sealed_dir, sandbox_job) == 0
    view = merge_store_features(
        CompletedBarsView.from_rows(rows),
        sandbox_job,
        spec=_declaring("leader_state", "btc_ret_7d"),
    )
    assert view.feature("leader_state") == 1.0
    assert view.feature("btc_ret_7d") > 0.08


def test_world_without_a_leader_file_records_no_leaders(tmp_path: Path) -> None:
    _, _, world_dir, sealed_dir, _ = _world(tmp_path)

    world = load_world(world_dir, sealed_dir)

    assert world["manifest"]["holdout_features"]["leaders"] is None
    assert "leader_state" not in (
        world["manifest"]["holdout_features"].get("names") or []
    )


def test_scorecard_counts_sequence_previews_and_stale_repairs() -> None:
    scorecard = _scorecard(
        state={
            "counts": {},
            "candidates": [
                {
                    "attempts": [
                        {
                            "postmortem": {"primary_failure": "activity_below_floor"},
                            "outcome": {
                                "quick_simulation_ran": True,
                                "sequence_preview": {"status": "armed_no_entry"},
                            },
                        },
                        {
                            "postmortem": {"primary_failure": "no_progress_preview"},
                            "outcome": {
                                "quick_simulation_ran": False,
                                "sequence_preview": {"status": "armed_no_entry"},
                            },
                        },
                    ]
                }
            ],
        },
        funnel={},
        forward={},
        holdout={},
        sessions=[],
        cost={},
    )

    assert scorecard["process"]["sequence_previews"] == 2
    assert scorecard["process"]["sequence_preview_frozen"] == 2
    assert scorecard["process"]["no_progress_preview_rejections"] == 1
    assert scorecard["process"]["quick_simulations"] == 1


def _bundle_with_spec(bundle: Path, mutate) -> None:
    """Rewrite a bundle's execution spec wherever it lives."""
    from wayfinder_paths.jobs.execution.validation import resolve_execution_spec

    job_data = yaml.safe_load((bundle / "job.yaml").read_text(encoding="utf-8"))
    spec_data, _ = resolve_execution_spec(bundle, job_data)
    spec_data = json.loads(json.dumps(spec_data or {}))
    mutate(spec_data)
    (bundle / "execution_spec.json").write_text(json.dumps(spec_data), encoding="utf-8")
    if "execution_spec" in job_data:
        job_data["execution_spec"] = spec_data
        (bundle / "job.yaml").write_text(yaml.safe_dump(job_data), encoding="utf-8")


def test_race_accepts_a_candidate_that_declares_a_feature(tmp_path: Path) -> None:
    from wayfinder_paths.jobs.bench.forward_replay import evaluate_bundle

    _, _, world_dir, sealed_dir, rows = _world(tmp_path)
    world = load_world(world_dir, sealed_dir)
    environment = world["manifest"]["execution_environment"]
    cutoff = datetime(2026, 1, 3, 23, tzinfo=UTC)

    declares = tmp_path / "declares"
    shutil.copytree(world_dir / "incumbent", declares)

    def declare(spec: dict) -> None:
        spec.setdefault("data_contract", {})["features"] = [
            {"name": "macro_regime", "source": "file"}
        ]

    _bundle_with_spec(declares, declare)
    verdict = evaluate_bundle(
        declares, rows=rows, cutoff=cutoff, environment=environment
    )
    assert verdict["spec_matches_world"] is True

    # A different market is still a different market.
    other = tmp_path / "other"
    shutil.copytree(world_dir / "incumbent", other)

    def retime(spec: dict) -> None:
        spec.setdefault("data_contract", {})["bar_interval"] = "4h"

    _bundle_with_spec(other, retime)
    assert (
        evaluate_bundle(other, rows=rows, cutoff=cutoff, environment=environment)[
            "spec_matches_world"
        ]
        is False
    )


def test_world_tolerates_a_declared_default_store_it_derives(tmp_path: Path) -> None:
    from wayfinder_paths.jobs.bench.world import install_development_world

    store, job_id, rows = _job(tmp_path / "source")
    source = store.job_dir(job_id)
    (source / "results/backtest/input_bars.json").write_text(
        json.dumps({"metadata": {"days": 40}, "bars": _bars(count=24 * 40)}),
        encoding="utf-8",
    )

    def declare(spec: dict) -> None:
        spec.setdefault("data_contract", {})["features"] = [
            {"name": "macro_regime", "source": "file"}
        ]

    _bundle_with_spec(source, declare)
    assert not (source / "state/features.jsonl").exists()
    manifest = prepare_world(
        source,
        tmp_path / "world",
        generation_cutoff=datetime(2026, 1, 25, 23, tzinfo=UTC),
        holdout_end=datetime(2026, 2, 9, 23, tzinfo=UTC),
        sealed_dir=tmp_path / "sealed",
        world_id="derived-store",
    )
    entry = manifest["features"][0]
    assert entry["missing_source"] is True and entry["rows"] > 0
    assert "macro_ret_7d" in entry["derived"]
    sandbox_job = tmp_path / "sandbox-job"
    sandbox_job.mkdir()
    install_development_world(tmp_path / "world", destination_job=sandbox_job)
    assert (sandbox_job / "state/features.jsonl").read_text().count("macro_ret_7d") > 0

    # A declared file elsewhere is still required.
    def elsewhere(spec: dict) -> None:
        spec.setdefault("data_contract", {})["features"] = [
            {
                "name": "funding",
                "source": "file",
                "path": "workspace/data/funding.jsonl",
            }
        ]

    _bundle_with_spec(source, elsewhere)
    with pytest.raises(FileNotFoundError, match="declared feature file is missing"):
        prepare_world(
            source,
            tmp_path / "world2",
            generation_cutoff=datetime(2026, 1, 25, 23, tzinfo=UTC),
            holdout_end=datetime(2026, 2, 9, 23, tzinfo=UTC),
            sealed_dir=tmp_path / "sealed2",
            world_id="missing-store",
        )


def test_load_world_verifies_the_sealed_feature_hash(tmp_path: Path) -> None:
    store, job_id, _ = _job(tmp_path / "source")
    source = store.job_dir(job_id)
    (source / "results/backtest/input_bars.json").write_text(
        json.dumps({"metadata": {"days": 40}, "bars": _bars(count=24 * 40)}),
        encoding="utf-8",
    )
    world_dir, sealed_dir = tmp_path / "world", tmp_path / "sealed"
    prepare_world(
        source,
        world_dir,
        generation_cutoff=datetime(2026, 1, 25, 23, tzinfo=UTC),
        holdout_end=datetime(2026, 2, 9, 23, tzinfo=UTC),
        sealed_dir=sealed_dir,
        world_id="sealed-check",
    )
    load_world(world_dir, sealed_dir)
    sealed = sealed_dir / "features-holdout.jsonl"
    sealed.write_text(sealed.read_text() + '{"tampered": true}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="sealed holdout features"):
        load_world(world_dir, sealed_dir)


def test_legacy_world_manifests_still_match_on_the_full_spec_hash(
    tmp_path: Path,
) -> None:
    from wayfinder_paths.jobs.bench.env import sha256_json
    from wayfinder_paths.jobs.bench.forward_replay import evaluate_bundle
    from wayfinder_paths.jobs.execution.validation import resolve_execution_spec

    _, _, world_dir, sealed_dir, rows = _world(tmp_path)
    manifest = load_world(world_dir, sealed_dir)["manifest"]
    environment = manifest["execution_environment"]
    cutoff = datetime(2026, 1, 3, 23, tzinfo=UTC)
    incumbent = world_dir / "incumbent"
    job_data = yaml.safe_load((incumbent / "job.yaml").read_text(encoding="utf-8"))
    spec_data, _ = resolve_execution_spec(incumbent, job_data)

    # New manifests carry both: the full hash keeps its old meaning.
    assert environment["spec_sha256"] == sha256_json(spec_data)
    assert environment[
        "execution_identity_sha256"
    ]  # equal to the full hash only when nothing is declared

    legacy = {"spec_sha256": sha256_json(spec_data), "params": environment["params"]}
    assert (
        evaluate_bundle(incumbent, rows=rows, cutoff=cutoff, environment=legacy)[
            "spec_matches_world"
        ]
        is True
    )
    declares = tmp_path / "declares"
    shutil.copytree(incumbent, declares)
    _bundle_with_spec(
        declares,
        lambda spec: spec.setdefault("data_contract", {}).__setitem__(
            "features", [{"name": "macro_regime", "source": "file"}]
        ),
    )
    # A world frozen before the identity existed keeps rejecting it, as it did.
    assert (
        evaluate_bundle(declares, rows=rows, cutoff=cutoff, environment=legacy)[
            "spec_matches_world"
        ]
        is False
    )
    assert (
        evaluate_bundle(declares, rows=rows, cutoff=cutoff, environment=environment)[
            "spec_matches_world"
        ]
        is True
    )


def _rising_bars(days: int) -> list[dict]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        {
            "timestamp": (start + timedelta(hours=index)).isoformat(),
            "symbol": "IMX",
            "open": 10.0 * 1.01 ** (index / 24),
            "high": 10.1 * 1.01 ** (index / 24),
            "low": 9.9 * 1.01 ** (index / 24),
            "close": 10.0 * 1.01 ** (index / 24),
            "volume": 100.0,
        }
        for index in range(24 * days)
    ]


def _feature_world(tmp_path: Path) -> tuple[Path, Path, Path, list[dict]]:
    """A 40-day rising world with a rising leader file, installed into a
    sandbox job with the holdout features revealed: macro +1 after 28 days,
    leader +1 after 7."""
    from wayfinder_paths.jobs.bench.leaders import LEADER_CLOSES_RELATIVE
    from wayfinder_paths.jobs.bench.world import (
        install_development_world,
        reveal_holdout_features,
    )

    store, job_id, _ = _job(tmp_path / "source")
    source = store.job_dir(job_id)
    bars = _rising_bars(40)
    (source / "results/backtest/input_bars.json").write_text(
        json.dumps({"metadata": {"days": 40}, "bars": bars}), encoding="utf-8"
    )
    closes = []
    for row in bars:
        level = 100.0 * 1.02 ** (bars.index(row) / 24) if False else None
    for index, row in enumerate(bars):
        level = 100.0 * 1.02 ** (index / 24)
        closes.append({"timestamp": row["timestamp"], "symbol": "BTC", "close": level})
        closes.append(
            {"timestamp": row["timestamp"], "symbol": "ETH", "close": level / 20}
        )
    (source / LEADER_CLOSES_RELATIVE).parent.mkdir(parents=True, exist_ok=True)
    (source / LEADER_CLOSES_RELATIVE).write_text(
        json.dumps({"metadata": {"interval": "1h"}, "closes": closes}), encoding="utf-8"
    )
    world_dir, sealed_dir = tmp_path / "world", tmp_path / "sealed"
    prepare_world(
        source,
        world_dir,
        generation_cutoff=datetime(2026, 1, 25, 23, tzinfo=UTC),
        holdout_end=datetime(2026, 2, 9, 23, tzinfo=UTC),
        sealed_dir=sealed_dir,
        world_id="feature-world",
    )
    sandbox_job = tmp_path / "sandbox-job"
    sandbox_job.mkdir()
    install_development_world(world_dir, destination_job=sandbox_job)
    reveal_holdout_features(sealed_dir, sandbox_job)
    return world_dir, sealed_dir, sandbox_job, bars


_READS_BOTH_FEATURES = (
    "def decide(ctx):\n"
    "    macro = ctx.view.feature('macro_regime', default=0.0)\n"
    "    leader = ctx.view.feature('leader_state', default=0.0)\n"
    "    if macro not in (-1.0, 0.0, 1.0) or leader not in (-1.0, 0.0, 1.0):\n"
    "        raise ValueError('unexpected feature code')\n"
    "    if str(ctx.timestamp) >= '2026-02-09T23' and (macro != 1.0 or leader != 1.0):\n"
    "        raise ValueError('declared features were never populated')\n"
    "    return []\n"
)


def test_race_replays_a_strategy_that_reads_declared_features_end_to_end(
    tmp_path: Path,
) -> None:
    from wayfinder_paths.jobs.bench.forward_replay import evaluate_bundle

    world_dir, sealed_dir, sandbox_job, rows = _feature_world(tmp_path)
    environment = load_world(world_dir, sealed_dir)["manifest"]["execution_environment"]
    bundle = tmp_path / "reads"
    shutil.copytree(world_dir / "incumbent", bundle)
    (bundle / "workspace/src/strategy.py").write_text(
        _READS_BOTH_FEATURES, encoding="utf-8"
    )
    _bundle_with_spec(
        bundle,
        lambda spec: spec.setdefault("data_contract", {}).__setitem__(
            "features",
            [
                {"name": "macro_regime", "source": "file"},
                {"name": "leader_state", "source": "file"},
            ],
        ),
    )
    verdict = evaluate_bundle(
        bundle,
        rows=rows,
        cutoff=datetime(2026, 1, 25, 23, tzinfo=UTC),
        environment=environment,
        feature_root=sandbox_job,
    )
    # The default carried the 28 feature-less days, the probe and the full
    # replay ran through the last bar, and both columns read +1 there.
    assert verdict["spec_matches_world"] is True
    assert verdict["window_invariance"]["status"] == "passed"
    assert verdict["validation"]["execution_valid"] is True
    assert verdict["valid"] is True


def test_race_does_not_merge_features_the_candidate_never_declared(
    tmp_path: Path,
) -> None:
    from wayfinder_paths.jobs.bench.forward_replay import evaluate_bundle

    world_dir, sealed_dir, sandbox_job, rows = _feature_world(tmp_path)
    environment = load_world(world_dir, sealed_dir)["manifest"]["execution_environment"]
    bundle = tmp_path / "undeclared"
    shutil.copytree(world_dir / "incumbent", bundle)
    (bundle / "workspace/src/strategy.py").write_text(
        _READS_BOTH_FEATURES, encoding="utf-8"
    )
    _bundle_with_spec(
        bundle,
        lambda spec: spec.setdefault("data_contract", {}).__setitem__(
            "features", [{"name": "macro_regime", "source": "file"}]
        ),
    )
    # leader_state sits in the store but was not declared: the replay must
    # fail the way the live driver would, not paper over it.
    with pytest.raises(ValueError, match="No feature column 'leader_state'"):
        evaluate_bundle(
            bundle,
            rows=rows,
            cutoff=datetime(2026, 1, 25, 23, tzinfo=UTC),
            environment=environment,
            feature_root=sandbox_job,
        )


def test_race_replay_carries_a_declared_feature_the_store_never_wrote(
    tmp_path: Path,
) -> None:
    """An absent store still yields the declared column, so the defaulted
    read carries the whole replay instead of raising 'No feature column'."""
    from wayfinder_paths.jobs.bench.forward_replay import evaluate_bundle

    _, _, world_dir, sealed_dir, rows = _world(tmp_path)
    environment = load_world(world_dir, sealed_dir)["manifest"]["execution_environment"]
    bundle = tmp_path / "defaulted"
    shutil.copytree(world_dir / "incumbent", bundle)
    (bundle / "workspace/src/strategy.py").write_text(
        "def decide(ctx):\n"
        "    if ctx.view.feature('macro_regime', default=0.0) != 0.0:\n"
        "        raise ValueError('nothing wrote this column')\n"
        "    return []\n",
        encoding="utf-8",
    )
    _bundle_with_spec(
        bundle,
        lambda spec: spec.setdefault("data_contract", {}).__setitem__(
            "features", [{"name": "macro_regime", "source": "file"}]
        ),
    )
    empty_store = tmp_path / "empty-job"
    empty_store.mkdir()
    verdict = evaluate_bundle(
        bundle,
        rows=rows,
        cutoff=datetime(2026, 1, 3, 23, tzinfo=UTC),
        environment=environment,
        feature_root=empty_store,
    )
    assert verdict["window_invariance"]["status"] == "passed"
    assert verdict["validation"]["execution_valid"] is True


def test_race_replay_honors_a_custom_feature_path_and_column_from_the_bundle(
    tmp_path: Path,
) -> None:
    from wayfinder_paths.jobs.bench.forward_replay import evaluate_bundle

    _, _, world_dir, sealed_dir, rows = _world(tmp_path)
    environment = load_world(world_dir, sealed_dir)["manifest"]["execution_environment"]
    bundle = tmp_path / "custom"
    shutil.copytree(world_dir / "incumbent", bundle)
    turned = rows[len(rows) // 2]["timestamp"]
    last = rows[-1]["timestamp"]
    (bundle / "workspace/data").mkdir(parents=True, exist_ok=True)
    (bundle / "workspace/data/custom.jsonl").write_text(
        json.dumps(
            {"timestamp": turned, "name": "signal", "value": 1.0, "symbol": None}
        )
        + "\n",
        encoding="utf-8",
    )
    (bundle / "workspace/src/strategy.py").write_text(
        "def decide(ctx):\n"
        "    sig = ctx.view.feature('sig', default=0.0)\n"
        f"    if str(ctx.timestamp) >= '{last[:13]}' and sig != 1.0:\n"
        "        raise ValueError('custom path or column mapping was lost')\n"
        "    return []\n",
        encoding="utf-8",
    )
    _bundle_with_spec(
        bundle,
        lambda spec: spec.setdefault("data_contract", {}).__setitem__(
            "features",
            [
                {
                    "name": "signal",
                    "source": "file",
                    "path": "workspace/data/custom.jsonl",
                    "column": "sig",
                }
            ],
        ),
    )
    verdict = evaluate_bundle(
        bundle,
        rows=rows,
        cutoff=datetime(2026, 1, 3, 23, tzinfo=UTC),
        environment=environment,
        feature_root=tmp_path / "no-store",
    )
    assert verdict["window_invariance"]["status"] == "passed"
    assert verdict["validation"]["execution_valid"] is True


def test_compose_stage_renders_as_a_designer_turn() -> None:
    from wayfinder_paths.jobs.worker import build_evolution_stage_prompt

    rendered = build_evolution_stage_prompt(
        "bench-job",
        {
            "campaign_id": "c1",
            "stage": "compose",
            "session_stage": "compose",
            "artifact_key": "compose-01",
            "agent_name": "wayfinder-evolution-designer",
            "deadline_at": "2099-01-01T00:00:00+00:00",
            "counts": {},
            "next_action": "propose signals",
            "constraints": {"composition": {"round": 1}},
        },
    )
    assert rendered["artifact_key"] == "compose-01"
    assert rendered["agent_name"] == "wayfinder-evolution-designer"
    assert rendered["work_order"]["lane"] == "evolution_compose"
    assert rendered["work_order"]["action"] == "submit_signal_proposals"
    assert "evolution_compose once" in rendered["work_order"]["completion"]
