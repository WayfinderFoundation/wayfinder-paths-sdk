from __future__ import annotations

import asyncio
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from wayfinder_paths.jobs.bench.aggregate import aggregate_experiment
from wayfinder_paths.jobs.bench.forward_replay import race_bundles, replay_probation
from wayfinder_paths.jobs.bench.identity import (
    compare_identities,
    ensure_model_declared,
)
from wayfinder_paths.jobs.bench.mcp_server import core_jobs as bench_core_jobs
from wayfinder_paths.jobs.bench.runner import (
    _assert_bench_root,
    _install_job,
    _validate_config,
)
from wayfinder_paths.jobs.bench.world import load_world, prepare_world
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


def test_race_is_deterministic_and_does_not_mutate_bundles(tmp_path: Path) -> None:
    _, _, world_dir, sealed_dir, _ = _world(tmp_path)
    a = world_dir / "incumbent"
    b = tmp_path / "bundle-b"
    shutil.copytree(a, b)
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
    assert first["paired_daily_utility_delta"] == second["paired_daily_utility_delta"]
    assert revisions == (
        compute_workspace_revision(a),
        compute_workspace_revision(b),
    )
    assert (output / "results/a/trades.json").exists()
    assert (output / "results/b/equity.json").exists()
    assert (output / "results/compare.json").exists()


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
    assert "submit the design" in rendered["prompt"]
    assert '"candidate_id": "c01"' in rendered["prompt"]


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
    other["data"] = base["data"]
    other["initial_prompt_sha256"] = "different-prompt"
    assert not compare_identities(base, other, allowed={"model", "variant"})[
        "comparable"
    ]


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
        }
        for seed in config["seeds"]
        for arm in ("a", "b")
    ]

    report = aggregate_experiment(rows, arm_order=["a", "b"])

    assert report["primary"]["pairs"] == 4
    assert report["cost_parity"]["matched"] is True
    assert report["decision"] == "a_wins"
