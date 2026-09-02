from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
from wayfinder_paths.jobs.benchmarks.agent_adapter import install_agent_workspace
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
