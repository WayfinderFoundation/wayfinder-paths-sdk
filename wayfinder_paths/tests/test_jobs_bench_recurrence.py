from __future__ import annotations

import json
import shutil
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import wayfinder_paths.jobs.bench.recurrence as recurrence_module
import wayfinder_paths.jobs.bench.runner as runner_module
from wayfinder_paths.jobs.application import apply_candidate_bundle
from wayfinder_paths.jobs.bench.recurrence import (
    _ideation_usage,
    _researcher_wake,
    _validate_recurrence_config,
    aggregate_recurrence,
    run_recurrence,
    run_recurrence_arm,
)
from wayfinder_paths.jobs.bench.world import prepare_world
from wayfinder_paths.jobs.execution import ExecutionSpec
from wayfinder_paths.jobs.gating import compute_workspace_revision
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.probation import (
    PROBATION_PATH,
    load_probation,
    stage_evolution_probation,
)
from wayfinder_paths.jobs.store import JobStore

FLAT = "def decide(ctx):\n    return []\n"
LONG_ONCE = (
    "def decide(ctx):\n"
    "    if ctx.ledger.positions.get('IMX'):\n"
    "        return []\n"
    "    return [{'action': 'OPEN', 'venue': 'hyperliquid', 'symbol': 'IMX', "
    "'side': 'buy', 'size': 10.0}]\n"
)
START = datetime(2026, 1, 1, tzinfo=UTC)


def _bars(count: int = 1_300) -> list[dict[str, Any]]:
    return [
        {
            "timestamp": (START + timedelta(hours=index)).isoformat(),
            "symbol": "IMX",
            "open": 10.0 + index * 0.01,
            "high": 10.1 + index * 0.01,
            "low": 9.9 + index * 0.01,
            "close": 10.0 + index * 0.01,
            "volume": 100.0,
        }
        for index in range(count)
    ]


def _source_job(root: Path, *, strategy: str = FLAT) -> Path:
    store = JobStore(repo_root=root)
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
    job_root = store.job_dir(job.id)
    (job_root / "workspace/src/strategy.py").write_text(strategy, encoding="utf-8")
    (job_root / "results/backtest/input_bars.json").write_text(
        json.dumps({"metadata": {"days": 54}, "bars": _bars()}), encoding="utf-8"
    )
    return job_root


def _config(source_job: Path, output: Path, **overrides: Any) -> dict[str, Any]:
    config = {
        "schema_version": "1.0",
        "kind": "recurrence",
        "pilot": True,
        "source_job": str(source_job),
        "output_dir": str(output),
        "window": {
            "start_cutoff": (START + timedelta(days=35)).isoformat(),
            "loop_days": 5,
            "loops": 3,
        },
        "campaign": {
            "probation": {
                "burn_in_hours": 0,
                "min_paired_days": 3,
                "max_paired_days": 4,
            }
        },
        "arms": [{"name": "evolve", "model": "test/model"}],
        "seeds": [7],
        "interface_smoke": False,
        "arm_timeout_seconds": 20_000,
        "_source_job": str(source_job),
        "_config_dir": str(output.parent),
        "_runtime_pins": {"arms": [{"name": "evolve", "sdk_ref": "deadbeef"}]},
    }
    config.update(overrides)
    return config


def _fake_prepare_sandbox(**kwargs: Any) -> dict[str, Any]:
    run_root = kwargs["output_dir"] / "workspaces" / kwargs["run_id"]
    store, job_id = runner_module._install_job(
        run_root,
        world_dir=kwargs["world_dir"],
        policy={
            **dict(kwargs["config"].get("campaign") or {}),
            **dict(kwargs["arm"].get("campaign") or {}),
        },
        job_id_override=kwargs["job_id"],
    )
    return {
        "run_id": kwargs["run_id"],
        "run_root": run_root,
        "store": store,
        "job_id": job_id,
        "model": "test/model",
        "variant": None,
        "opencode": Path("/nonexistent/opencode"),
        "port": 0,
        "sdk_root": run_root,
        "arm_campaign": dict(kwargs["arm"].get("campaign") or {}),
    }


def _fake_audit(sandbox: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    staged = 1 if (kwargs["forward"] or {}).get("available") else 0
    return {
        "cost": {"tokens_in": 10, "tokens_out": 2, "wall_seconds": 1.0},
        "isolation": {"passed": True, "denied_attempts": []},
        "invalid_reason": kwargs["invalid_reason"],
        "state": {},
        "scorecard": {
            "funnel": {"candidates_generated": 1, "attempts": 1, "staged": staged},
            "verdicts": {},
            "diversity": {"qd_cells_occupied": 1},
            "process": {"turns": 1},
        },
        "session_diagnostics": [],
    }


def _stage_candidate(
    sandbox: dict[str, Any], *, campaign_id: str, strategy: str, now: datetime
) -> None:
    store: JobStore = sandbox["store"]
    job_id = str(sandbox["job_id"])
    root = store.job_dir(job_id)
    candidate_root = root / "research/evolution/campaigns" / campaign_id / "cand-1"
    shutil.copytree(root / "workspace", candidate_root / "workspace")
    shutil.copy2(root / "job.yaml", candidate_root / "job.yaml")
    (candidate_root / "workspace/src/strategy.py").write_text(
        strategy, encoding="utf-8"
    )
    staged = stage_evolution_probation(
        store,
        job_id,
        candidate_id="cand-1",
        candidate_root=candidate_root,
        revision=compute_workspace_revision(candidate_root),
        source="de_novo",
        family="long-once",
        campaign_id=campaign_id,
        now=now,
    )
    assert staged["trial_id"]


def _write_campaign_state(sandbox: dict[str, Any], campaign_id: str) -> None:
    store: JobStore = sandbox["store"]
    store.write_json(
        str(sandbox["job_id"]),
        "state/evolution_campaign.json",
        {
            "campaign_id": campaign_id,
            "status": "complete",
            "candidates": [],
            "counts": {},
        },
    )


def _graduating_replay(store: JobStore, job_id: str, **kwargs: Any) -> dict[str, Any]:
    doc = load_probation(store, job_id)
    trial = next(
        (
            row
            for row in doc["trials"]
            if row.get("campaign_id") == kwargs.get("campaign_id")
            and row.get("status") in {"queued", "burn_in", "active"}
        ),
        None,
    )
    if trial is None:
        return {
            "available": False,
            "reason": "campaign staged no probation candidate",
            "paired_daily_delta": [],
        }
    trial["status"] = "graduated"
    trial["phase"] = "graduated"
    store.write_json(job_id, PROBATION_PATH, doc)
    return {
        "available": True,
        "trial_id": trial["trial_id"],
        "candidate_id": trial["candidate_id"],
        "status": "graduated",
        "phase": "graduated",
        "burn_in": {},
        "forward": {"metrics": {}},
        "paired_daily_delta": [],
    }


def _patch_common(monkeypatch: pytest.MonkeyPatch) -> None:
    # The arm pins the benchmark clock in its own process; keep it out of
    # this one so later tests see the production clock.
    monkeypatch.setenv("WAYFINDER_BENCHMARK", "0")
    monkeypatch.setenv("WAYFINDER_BENCHMARK_NOW", "")
    monkeypatch.setattr(recurrence_module, "prepare_sandbox", _fake_prepare_sandbox)
    monkeypatch.setattr(
        recurrence_module, "bench_mcp_server", lambda sandbox, *, env: nullcontext()
    )
    monkeypatch.setattr(recurrence_module, "audit_and_score", _fake_audit)
    monkeypatch.setattr(
        recurrence_module,
        "runtime_identity",
        lambda **kwargs: {"same": True, "seed": kwargs["repeat_seed"]},
    )
    monkeypatch.setattr(runner_module, "replay_probation", _graduating_replay)


def test_recurrence_chains_loops_and_applies_graduate_with_one_loop_lag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source_job(tmp_path / "source")
    output = tmp_path / "out"
    config = _config(source, output)
    _patch_common(monkeypatch)
    campaigns: list[str] = []

    def fake_campaign(sandbox: dict[str, Any], **kwargs: Any) -> str | None:
        campaign_id = f"camp-{len(campaigns)}"
        campaigns.append(campaign_id)
        _write_campaign_state(sandbox, campaign_id)
        if campaign_id == "camp-0":
            _stage_candidate(
                sandbox,
                campaign_id=campaign_id,
                strategy=LONG_ONCE,
                now=kwargs["virtual_now"],
            )
        return None

    monkeypatch.setattr(recurrence_module, "run_campaign_phase", fake_campaign)

    row = run_recurrence_arm(
        config=config, arm=config["arms"][0], seed=7, output_dir=output
    )

    loops = row["loops"]
    assert [loop["invalid_reason"] for loop in loops] == [None, None, None]
    assert campaigns == ["camp-0", "camp-1", "camp-2"]
    # Loop 0: the deployed strategy is the frozen original, so the endpoint is
    # zero by construction; the graduate applies afterwards.
    assert loops[0]["forward"]["days"] > 0
    assert all(value == 0 for value in loops[0]["forward"]["paired_deltas"])
    assert loops[0]["apply"]["applied"] is True
    assert loops[0]["probation_carried"] == []
    assert loops[0]["holdout"]["verdict"] in {
        "invalid",
        "a_beats_b",
        "no_significant_difference",
    }
    # Loop 1 deploys the graduate: its revision is the incumbent and the
    # long-once strategy diverges from the flat original on a rising tape.
    graduate = loops[0]["apply"]["trials"][0]
    assert loops[1]["incumbent_revision"] == graduate["candidate_revision"]
    assert loops[1]["incumbent_revision"] != loops[0]["incumbent_revision"]
    assert loops[1]["frozen_revision"] == loops[0]["incumbent_revision"]
    assert any(value != 0 for value in loops[1]["forward"]["paired_deltas"])
    assert loops[1]["apply"]["applied"] is False
    assert row["lineage"]["depth"] == 1
    assert row["lineage"]["chain"][0]["applied"] is True
    assert row["lineage"]["chain"][0]["trial_ids"] == [graduate["trial_id"]]
    assert row["dynamics"] == {
        "loops": 3,
        "staged": 1,
        "graduated": 1,
        "killed": 0,
        "inconclusive": 0,
        "applied": 1,
        "time_to_first_apply": 0,
        "false_applies": 0,
        "invalid_loops": 0,
    }
    assert row["holdout"]["paired_daily_utility_delta"]["days"] == sum(
        loop["forward"]["days"] for loop in loops
    )
    job_root = output / "workspaces" / "evolve-7" / ".wayfinder" / "jobs"
    summary_paths = list(job_root.glob("*/results/forward/summary.json"))
    assert len(summary_paths) == 1
    summary = json.loads(summary_paths[0].read_text(encoding="utf-8"))
    assert summary["status"] == "replayed" and summary["mode"] == "paper"
    assert summary["runs"]["count"] == 0
    assert (output / "loops" / "evolve-7" / "loop-2" / "loop.json").exists()
    assert (output / "sealed" / "evolve-7" / "loop-1" / "holdout.json").exists()


def test_recurrence_carries_open_probation_into_the_next_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source_job(tmp_path / "source")
    output = tmp_path / "out"
    config = _config(source, output)
    _patch_common(monkeypatch)
    campaigns: list[str] = []
    replays: list[str | None] = []

    def fake_campaign(sandbox: dict[str, Any], **kwargs: Any) -> str | None:
        campaign_id = f"camp-{len(campaigns)}"
        campaigns.append(campaign_id)
        _write_campaign_state(sandbox, campaign_id)
        if campaign_id == "camp-0":
            _stage_candidate(
                sandbox,
                campaign_id=campaign_id,
                strategy=LONG_ONCE,
                now=kwargs["virtual_now"],
            )
        return None

    def carrying_replay(store: JobStore, job_id: str, **kwargs: Any) -> dict[str, Any]:
        campaign_id = kwargs.get("campaign_id")
        replays.append(campaign_id)
        doc = load_probation(store, job_id)
        open_trials = [
            row
            for row in doc["trials"]
            if row.get("status") in {"queued", "burn_in", "active"}
        ]
        own = next(
            (row for row in open_trials if row.get("campaign_id") == campaign_id),
            None,
        )
        if own is not None:
            # Loop 0: the trial clears burn-in but the window ends before
            # a verdict, so it stays open.
            own["status"] = "active"
            own["phase"] = "forward"
            store.write_json(job_id, PROBATION_PATH, doc)
            return {
                "available": True,
                "trial_id": own["trial_id"],
                "candidate_id": own["candidate_id"],
                "status": "active",
                "phase": "forward",
                "burn_in": {},
                "forward": {"metrics": {}},
                "carried": [],
                "paired_daily_delta": [],
            }
        # Later loops stage nothing; the carried trial reaches its verdict.
        carried = [str(row["trial_id"]) for row in open_trials]
        for row in open_trials:
            row["status"] = "graduated"
            row["phase"] = "graduated"
        store.write_json(job_id, PROBATION_PATH, doc)
        return {
            "available": False,
            "reason": "campaign staged no probation candidate",
            "carried": carried,
            "paired_daily_delta": [],
        }

    monkeypatch.setattr(recurrence_module, "run_campaign_phase", fake_campaign)
    monkeypatch.setattr(runner_module, "replay_probation", carrying_replay)

    row = run_recurrence_arm(
        config=config, arm=config["arms"][0], seed=7, output_dir=output
    )

    loops = row["loops"]
    assert [loop["invalid_reason"] for loop in loops] == [None, None, None]
    assert replays == ["camp-0", "camp-1", "camp-2"]
    trial_id = loops[0]["probation"]["trial_id"]
    assert loops[0]["probation"]["status"] == "active"
    assert loops[0]["apply"] == {"applied": False, "trials": []}
    assert loops[0]["probation_carried"] == [trial_id]
    # Loop 1 deploys the still-flat incumbent, then applies the carried graduate.
    assert loops[1]["incumbent_revision"] == loops[0]["incumbent_revision"]
    assert all(value == 0 for value in loops[1]["forward"]["paired_deltas"])
    assert loops[1]["apply"]["applied"] is True
    assert [trial["trial_id"] for trial in loops[1]["apply"]["trials"]] == [trial_id]
    assert loops[1]["probation_carried"] == []
    graduate = loops[1]["apply"]["trials"][0]
    assert loops[2]["incumbent_revision"] == graduate["candidate_revision"]
    assert loops[2]["incumbent_revision"] != loops[0]["incumbent_revision"]
    assert any(value != 0 for value in loops[2]["forward"]["paired_deltas"])
    assert loops[2]["apply"] == {"applied": False, "trials": []}
    assert row["lineage"]["depth"] == 1
    assert row["lineage"]["chain"][0]["applied"] is False
    assert row["lineage"]["chain"][1]["trial_ids"] == [trial_id]
    assert row["lineage"]["chain"][1]["candidate_ids"] == ["cand-1"]
    assert row["dynamics"]["applied"] == 1
    assert row["dynamics"]["time_to_first_apply"] == 1
    assert row["dynamics"]["staged"] == 1


def test_recurrence_loop_failure_keeps_incumbent_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source_job(tmp_path / "source")
    output = tmp_path / "out"
    config = _config(source, output)
    _patch_common(monkeypatch)
    calls: list[int] = []

    def flaky_campaign(sandbox: dict[str, Any], **kwargs: Any) -> str | None:
        calls.append(len(calls))
        _write_campaign_state(sandbox, f"camp-{len(calls)}")
        if len(calls) == 2:
            raise RuntimeError("opencode exploded")
        return None

    monkeypatch.setattr(recurrence_module, "run_campaign_phase", flaky_campaign)

    row = run_recurrence_arm(
        config=config, arm=config["arms"][0], seed=7, output_dir=output
    )

    loops = row["loops"]
    assert len(loops) == 3 and calls == [0, 1, 2]
    assert loops[1]["invalid_reason"].startswith("RuntimeError: opencode exploded")
    assert loops[0]["invalid_reason"] is None and loops[2]["invalid_reason"] is None
    assert row["invalid_reason"] is None
    assert row["invalid_loops"] == [1]
    revisions = {loop["incumbent_revision"] for loop in loops}
    assert len(revisions) == 1
    assert row["dynamics"]["applied"] == 0
    assert row["holdout"]["paired_daily_utility_delta"]["estimate"] == 0.0


def _row(
    arm: str, seed: int, *, values: list[float], applied: int, staged: int = 1
) -> dict[str, Any]:
    return {
        "run_id": f"{arm}-{seed}",
        "arm": arm,
        "world_id": "w",
        "seed": seed,
        "invalid_reason": None,
        "holdout": {
            "paired_daily_utility_delta": {
                "days": len(values),
                "estimate": sum(values),
                "lcb": None,
                "values": values,
            }
        },
        "dynamics": {
            "staged": staged,
            "graduated": applied,
            "applied": applied,
            "false_applies": 0,
            "invalid_loops": 0,
        },
        "lineage": {"depth": applied},
        "loops": [
            {
                "loop": index,
                "invalid_reason": None,
                "forward": {"estimate": value},
                "campaign": {"funnel": {"staged": staged if index == 0 else 0}},
                "probation": {
                    "status": "graduated" if applied and index == 0 else None
                },
                "apply": {"applied": bool(applied and index == 0)},
            }
            for index, value in enumerate(values)
        ],
        "researcher": {"enabled": False},
        "cost": {"tokens_in": 100, "tokens_out": 10, "wall_seconds": 5},
        "funnel": {"staged": staged},
        "forward": {"status": None},
        "process": {},
        "diversity": {},
        "isolation": {"denied_attempts": []},
    }


def test_recurrence_aggregate_reports_no_applies_when_all_deltas_zero(
    tmp_path: Path,
) -> None:
    rows = [_row("evolve", seed, values=[0.0, 0.0, 0.0], applied=0) for seed in (1, 2)]

    report = aggregate_recurrence(rows, arm_order=["evolve"], output_dir=tmp_path)

    arm = report["by_arm"]["evolve"]
    assert arm["direction"] == "no_applies" and arm["decision"] == "no_applies"
    assert arm["estimate"] == 0.0 and arm["applies"] == 0
    assert [cell["nonzero_days"] for cell in arm["per_seed"]] == [0, 0]
    assert report["by_loop"][0]["evolve"]["staged"] == 2
    assert "pairwise" not in report
    assert "no_applies" in (tmp_path / "report.txt").read_text(encoding="utf-8")


def test_recurrence_aggregate_pairs_process_vs_frozen_with_applies(
    tmp_path: Path,
) -> None:
    rows = [
        _row("evolve", seed, values=[0.0, 0.02, 0.03], applied=1)
        for seed in (1, 2, 3, 4)
    ] + [
        _row("control", seed, values=[0.0, 0.0, 0.0], applied=0)
        for seed in (1, 2, 3, 4)
    ]

    report = aggregate_recurrence(
        rows, arm_order=["evolve", "control"], output_dir=tmp_path
    )

    evolve = report["by_arm"]["evolve"]
    assert evolve["applies"] == 4 and evolve["lcb"] is not None and evolve["lcb"] > 0
    assert evolve["direction"] == "process_beats_frozen"
    assert report["by_arm"]["control"]["direction"] == "no_applies"
    assert report["by_loop"][1]["evolve"]["applied"] == 0
    assert report["by_loop"][0]["evolve"]["applied"] == 4
    assert report["pairwise"]["primary"]["pairs"] == 4
    assert report["pairwise"]["decision"] == "a_wins"


def test_recurrence_config_validation(tmp_path: Path) -> None:
    source = _source_job(tmp_path / "source")
    output = tmp_path / "out"

    _validate_recurrence_config(_config(source, output), source_job=source)

    # Production probation (24h burn-in, 14 paired days, partial cutoff day)
    # needs 16 days: three 5-day loops fall short, three 7-day loops do not,
    # because probation carries across loops.
    with pytest.raises(ValueError, match="probation cannot reach a verdict"):
        _validate_recurrence_config(
            _config(source, output, campaign={}), source_job=source
        )
    _validate_recurrence_config(
        _config(
            source,
            output,
            campaign={},
            window={
                "start_cutoff": (START + timedelta(days=35)).isoformat(),
                "loop_days": 7,
                "loops": 3,
            },
        ),
        source_job=source,
    )
    with pytest.raises(ValueError, match="built-in control"):
        _validate_recurrence_config(
            _config(source, output, arms=[{"name": "frozen", "model": "m"}]),
            source_job=source,
        )
    with pytest.raises(ValueError, match="at least four seeds"):
        _validate_recurrence_config(
            _config(source, output, pilot=False), source_job=source
        )
    with pytest.raises(ValueError, match="one or two arms"):
        _validate_recurrence_config(
            _config(
                source,
                output,
                arms=[{"name": n, "model": "m"} for n in ("a", "b", "c")],
            ),
            source_job=source,
        )
    with pytest.raises(ValueError, match="an hour per loop"):
        _validate_recurrence_config(
            _config(source, output, arm_timeout_seconds=600), source_job=source
        )
    with pytest.raises(FileNotFoundError, match="missing job.yaml"):
        _validate_recurrence_config(
            _config(source, output), source_job=tmp_path / "missing"
        )


def test_world_holdout_guard_is_parameterized(tmp_path: Path) -> None:
    source = _source_job(tmp_path / "source")
    cutoff = START + timedelta(days=35)

    with pytest.raises(ValueError, match="14 to 21 days"):
        prepare_world(
            source,
            tmp_path / "world-a",
            generation_cutoff=cutoff,
            holdout_end=cutoff + timedelta(days=5),
            sealed_dir=tmp_path / "sealed-a",
        )
    manifest = prepare_world(
        source,
        tmp_path / "world-b",
        generation_cutoff=cutoff,
        holdout_end=cutoff + timedelta(days=5),
        sealed_dir=tmp_path / "sealed-b",
        min_holdout_days=5,
    )
    assert manifest["holdout_bars"] == 120


def test_apply_candidate_bundle_swaps_workspace_and_keeps_operator_dials(
    tmp_path: Path,
) -> None:
    source = _source_job(tmp_path / "source")
    store = JobStore(repo_root=tmp_path / "source")
    job_id = source.name
    candidate = tmp_path / "candidate"
    shutil.copytree(source / "workspace", candidate / "workspace")
    shutil.copy2(source / "job.yaml", candidate / "job.yaml")
    (candidate / "workspace/src/strategy.py").write_text(LONG_ONCE, encoding="utf-8")
    candidate_job = json.loads(json.dumps(store.load(job_id).to_dict()))
    candidate_job["execution_params"]["initial_capital"] = 1.0
    import yaml

    (candidate / "job.yaml").write_text(yaml.safe_dump(candidate_job), encoding="utf-8")

    result = apply_candidate_bundle(store, job_id, candidate, label="recur-test")

    assert (source / "workspace/src/strategy.py").read_text(
        encoding="utf-8"
    ) == LONG_ONCE
    assert store.load(job_id).execution_params["initial_capital"] == 10_000.0
    assert result["promoted_revision"] == compute_workspace_revision(source)
    assert (
        source / "applications/recur-test/backup/workspace/src/strategy.py"
    ).read_text(encoding="utf-8") == FLAT


def test_recurrence_runs_arm_seed_chains_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source_job(tmp_path / "source")
    output = tmp_path / "out"
    config_path = tmp_path / "recurrence.json"
    config = {
        key: value
        for key, value in _config(source, output, seeds=[1, 2]).items()
        if not key.startswith("_")
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(recurrence_module, "_recurrence_pins", lambda *args, **kw: {})

    def fake_chain(*, arm: dict[str, Any], seed: int, run_id: str, **kwargs: Any):
        assert kwargs["request_extra"] == {"kind": "recurrence"}
        assert kwargs["world_dir"] is None
        return _row(str(arm["name"]), seed, values=[0.0, 0.01, 0.0], applied=1) | {
            "run_id": run_id,
            "identity": {"same": True},
        }

    monkeypatch.setattr(recurrence_module, "_run_arm_in_declared_sdk", fake_chain)

    report = run_recurrence(config_path)

    assert sorted(path.name for path in (output / "runs").glob("*.json")) == [
        "evolve-1.json",
        "evolve-2.json",
    ]
    assert report["by_arm"]["evolve"]["applies"] == 2
    assert report["by_arm"]["evolve"]["decision"] == "pilot_directional_only"
    assert (output / "recurrence.json").exists()
    assert (output / "aggregate.json").exists()


_CLOCK = "2026-08-10T15:25:00+00:00"


def _wake_sandbox(tmp_path: Path) -> dict[str, Any]:
    store = JobStore(repo_root=tmp_path / "sandbox")
    job = WayfinderJob.new("bench-majors-live-s7", script="workspace/src/strategy.py")
    store.save(job)
    return {
        "run_id": "evolve-7",
        "run_root": tmp_path / "sandbox",
        "store": store,
        "job_id": job.id,
        "model": "test/model",
        "variant": None,
        "opencode": Path("/nonexistent/opencode"),
    }


def _valid_artifact(store: JobStore, job_id: str, *, generated_at: str) -> None:
    path = store.job_dir(job_id) / "research" / "ideation" / "latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "generated_at": generated_at,
                "sources_consulted": [
                    {
                        "tool": f"results/research/f{i}.json",
                        "query": "q",
                        "takeaway": "t",
                    }
                    for i in range(3)
                ],
                "hypotheses": [
                    {
                        "title": f"H{i}",
                        "thesis": "x",
                        "bucket": "testable",
                        "next_step": "s",
                    }
                    for i in range(3)
                ],
            }
        ),
        encoding="utf-8",
    )


def test_researcher_wake_retries_once_with_the_contract_gaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = _wake_sandbox(tmp_path)
    store, job_id = sandbox["store"], sandbox["job_id"]
    corrections: list[dict[str, Any]] = []
    monkeypatch.setattr(
        recurrence_module,
        "run_agent_wakes",
        lambda **kwargs: [
            {"title": kwargs["title"], "session_id": "ses-1", "exit_code": 0}
        ],
    )

    def fake_correction(**kwargs: Any) -> dict[str, Any]:
        corrections.append(kwargs)
        _valid_artifact(store, job_id, generated_at=_CLOCK)
        return {
            "title": kwargs["title"],
            "session_id": kwargs["session_id"],
            "exit_code": 0,
        }

    monkeypatch.setattr(recurrence_module, "run_agent_prompt", fake_correction)
    sessions: list[dict[str, Any]] = []

    report = _researcher_wake(
        sandbox,
        config={"researcher": {"timeout_seconds": 5}},
        env={"WAYFINDER_BENCHMARK_NOW": _CLOCK},
        loop=0,
        sessions=sessions,
    )

    assert report["retried"] is True and report["ideation_artifact"]["valid"] is True
    assert corrections[0]["session_id"] == "ses-1"
    assert "research/ideation/latest.json is missing" in corrections[0]["prompt"]
    assert f'"generated_at": "{_CLOCK}"' in corrections[0]["prompt"]
    assert [row["stage"] for row in sessions] == ["research-wake", "research-wake-fix"]


def test_researcher_wake_with_a_stale_clock_fails_only_when_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = _wake_sandbox(tmp_path)
    store, job_id = sandbox["store"], sandbox["job_id"]
    _valid_artifact(store, job_id, generated_at="2026-08-01T00:00:00+00:00")
    monkeypatch.setattr(
        recurrence_module,
        "run_agent_wakes",
        lambda **kwargs: [
            {"title": kwargs["title"], "session_id": None, "exit_code": 0}
        ],
    )
    monkeypatch.setattr(
        recurrence_module,
        "run_agent_prompt",
        lambda **kwargs: {"title": kwargs["title"], "session_id": None, "exit_code": 0},
    )

    report = _researcher_wake(
        sandbox,
        config={"researcher": {}},
        env={"WAYFINDER_BENCHMARK_NOW": _CLOCK},
        loop=1,
        sessions=[],
    )
    assert report["retried"] is True and report["ideation_artifact"]["valid"] is False
    assert report["ideation_artifact"]["problems"] == [
        f"generated_at must equal the wake clock {_CLOCK}"
    ]
    with pytest.raises(RuntimeError, match="researcher artifact invalid"):
        _researcher_wake(
            sandbox,
            config={"researcher": {"required": True}},
            env={"WAYFINDER_BENCHMARK_NOW": _CLOCK},
            loop=1,
            sessions=[],
        )


def test_ideation_usage_counts_design_hypotheses_citing_the_artifact(
    tmp_path: Path,
) -> None:
    sandbox = _wake_sandbox(tmp_path)
    store, job_id = sandbox["store"], sandbox["job_id"]
    store.write_json(
        job_id,
        "design.json",
        {
            "hypotheses": [
                {"evidence_refs": ["/research_ideation/hypotheses/0/title"]},
                {"evidence_refs": ["/baseline/stats/net_return"]},
            ]
        },
    )
    assert _ideation_usage(store, job_id, {"campaign_design": "design.json"}) == 1
    assert _ideation_usage(store, job_id, {}) == 0
