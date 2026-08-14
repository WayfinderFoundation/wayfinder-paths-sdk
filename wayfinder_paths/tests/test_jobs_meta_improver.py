"""Improver A/B harness: paired campaign construction (variant overrides
applied per sandbox), descendant-productivity scoring from harvested job
dirs, and the CI-gated ship verdict."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import yaml

from wayfinder_paths.jobs.benchmarks.meta_improver import (
    ImproverVariant,
    ab_verdict,
    build_paired_campaign,
    run_paired_campaign,
    score_paired_campaign,
)
from wayfinder_paths.jobs.constitution import CONSTITUTION_FILENAME
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore

_WORLDS = [SimpleNamespace(world_id="w1"), SimpleNamespace(world_id="w2")]
_GENOMES = {"w1": object(), "w2": object()}

STOCK = ImproverVariant(name="stock", description="production harness as-is")
TUNED = ImproverVariant(
    name="tuned",
    description="stricter gate + exploration boost",
    agent_definition="# tuned worker\nExplore harder.\n",
    constitution_overrides={"verdict": {"minimum_days": 1.0}},
    execution_param_overrides={"exploration": {"epsilon": 0.25}},
)


def _stub_bundle_builder(world, *, sandbox, repo_root, initial_genome) -> str:
    """Minimal stand-in for build_world_bundle: a real JobStore job dir with
    a job.yaml, no SDK tree copy."""
    store = JobStore(repo_root=sandbox)
    job = WayfinderJob.new(f"wob-{world.world_id}", agent_mode="intervene")
    store.save(job)
    root = store.job_dir(job.id)
    job_yaml_path = root / "job.yaml"
    job_yaml = yaml.safe_load(job_yaml_path.read_text()) or {}
    job_yaml["execution_params"] = {"genome_spec": {"genome": "seed"}}
    job_yaml_path.write_text(yaml.safe_dump(job_yaml, sort_keys=False))
    return job.id


def _campaign(tmp_path):
    return build_paired_campaign(
        _WORLDS,
        STOCK,
        TUNED,
        campaign_dir=tmp_path / "campaign",
        repo_root=tmp_path,
        initial_genomes=_GENOMES,
        wakes=4,
        model="test-model",
        bundle_builder=_stub_bundle_builder,
    )


def test_build_paired_campaign_applies_variants(tmp_path) -> None:
    manifest = _campaign(tmp_path)

    assert manifest["arm_order"] == ["stock", "tuned"]
    assert len(manifest["pairs"]) == 4  # 2 arms x 2 worlds
    assert manifest["budget"] == {"wakes": 4, "model": "test-model", "timeout_s": 1800}
    assert (tmp_path / "campaign" / "manifest.json").exists()
    fp_stock = manifest["arms"]["stock"]["fingerprint"]
    fp_tuned = manifest["arms"]["tuned"]["fingerprint"]
    assert fp_stock != fp_tuned

    # The tuned arm's overrides landed in ITS sandboxes only.
    tuned_pair = next(
        p for p in manifest["pairs"] if p["arm"] == "tuned" and p["world_id"] == "w1"
    )
    sandbox = tmp_path / "campaign" / "arms" / "tuned" / "w1"
    root = JobStore(repo_root=sandbox).job_dir(tuned_pair["job_id"])
    constitution = yaml.safe_load((root / CONSTITUTION_FILENAME).read_text())
    assert constitution["verdict"]["minimum_days"] == 1.0
    job_yaml = yaml.safe_load((root / "job.yaml").read_text())
    assert job_yaml["execution_params"]["exploration"]["epsilon"] == 0.25
    assert job_yaml["execution_params"]["genome_spec"] == {"genome": "seed"}
    agent_md = sandbox / ".opencode" / "agents" / "wayfinder-job-worker.md"
    assert "Explore harder" in agent_md.read_text()
    variant_stamp = json.loads((root / "meta_variant.json").read_text())
    assert variant_stamp == {"name": "tuned", "fingerprint": fp_tuned}

    stock_pair = next(
        p for p in manifest["pairs"] if p["arm"] == "stock" and p["world_id"] == "w1"
    )
    stock_root = JobStore(
        repo_root=tmp_path / "campaign" / "arms" / "stock" / "w1"
    ).job_dir(stock_pair["job_id"])
    assert not (stock_root / CONSTITUTION_FILENAME).exists()

    # Same worlds + variants + budget -> same campaign id, wherever it lives.
    again = build_paired_campaign(
        _WORLDS,
        STOCK,
        TUNED,
        campaign_dir=tmp_path / "elsewhere",
        repo_root=tmp_path,
        initial_genomes=_GENOMES,
        wakes=4,
        model="test-model",
        bundle_builder=_stub_bundle_builder,
    )
    assert again["campaign_id"] == manifest["campaign_id"]


def test_run_paired_campaign_interleaves_and_persists(tmp_path) -> None:
    manifest = _campaign(tmp_path)
    calls: list[tuple[str, str]] = []

    def fake_wakes(*, sandbox, job_id, wakes, model, opencode, timeout_s):
        # job ids collide across arms (same job name per world) — the sandbox
        # is the unique key.
        pair = next(p for p in manifest["pairs"] if p["sandbox"] == str(sandbox))
        calls.append((pair["world_id"], pair["arm"]))
        return [{"wake": 0, "title": f"t-{pair['arm']}-{job_id}", "exit_code": 0}]

    def fake_meter(sessions, *, session_db):
        return {"tokens_out": 1000}

    records = run_paired_campaign(manifest, wake_runner=fake_wakes, meter=fake_meter)
    # Arm-interleaved per world: A then B on w1, A then B on w2.
    assert calls == [("w1", "stock"), ("w1", "tuned"), ("w2", "stock"), ("w2", "tuned")]
    assert all(r["tokens"] == {"tokens_out": 1000} for r in records)
    runs_file = tmp_path / "campaign" / "runs.jsonl"
    assert len(runs_file.read_text().splitlines()) == 4


def _seed_arm_artifacts(
    manifest, *, arm: str, promoted: int, hurt: int, growths: list[float]
) -> None:
    for pair in manifest["pairs"]:
        if pair["arm"] != arm:
            continue
        store = JobStore(repo_root=Path(pair["sandbox"]))
        job_id = pair["job_id"]
        root = store.job_dir(job_id)
        proposals_dir = root / "proposals"
        proposals_dir.mkdir(exist_ok=True)
        for i in range(promoted + 1):
            applied = i < promoted
            (proposals_dir / f"p{i}.json").write_text(
                json.dumps(
                    {
                        "proposal_id": f"p{i}",
                        "application": {"status": "applied"} if applied else {},
                    }
                )
            )
        verdicts = {
            f"p{i}": {"verdict": "hurt" if i < hurt else "beat"}
            for i in range(promoted)
        }
        store.write_json(job_id, "state/promotion_verdicts.json", verdicts)
        store.write_json(
            job_id,
            "state/archive.json",
            {
                "candidates": [
                    {
                        "candidate_id": f"c{i}",
                        "created_at": f"2026-01-0{i + 1}T00:00:00Z",
                        "objective": {"net_log_growth": growth},
                    }
                    for i, growth in enumerate(growths)
                ]
            },
        )
        trials_path = root / "results" / "backtest" / "trials.jsonl"
        trials_path.parent.mkdir(parents=True, exist_ok=True)
        trials_path.write_text(
            "\n".join(
                json.dumps({"params": {"x": i}, "behavior": {"net_return": 0.01 * i}})
                for i in range(4)
            )
            + "\n"
        )


def test_score_paired_campaign_and_ship_verdict(tmp_path) -> None:
    manifest = _campaign(tmp_path)
    # Stock: 3 promotions (1 later judged hurt), climbing archive. Tuned: 1
    # promotion, flat archive — stock strictly dominates on productivity.
    _seed_arm_artifacts(
        manifest, arm="stock", promoted=3, hurt=1, growths=[0.0, 0.05, 0.12]
    )
    _seed_arm_artifacts(manifest, arm="tuned", promoted=1, hurt=0, growths=[0.0, 0.01])
    run_records = [
        {"world_id": p["world_id"], "arm": p["arm"], "tokens": {"tokens_out": 2000}}
        for p in manifest["pairs"]
    ]

    report = score_paired_campaign(manifest, run_records=run_records)
    stock_w1 = next(
        r for r in report["per_pair"] if r["arm"] == "stock" and r["world_id"] == "w1"
    )
    assert stock_w1["promoted"] == 3
    assert stock_w1["promoted_per_100_wakes"] == 75.0
    assert stock_w1["false_promotions"] == 1
    assert stock_w1["beats"] == 2
    assert stock_w1["utility_after_gen_1"] == 0.0
    assert stock_w1["utility_after_gen_3"] == 0.12
    assert stock_w1["utility_gain"] == 0.12
    assert stock_w1["utility_per_1k_tokens"] == 0.06
    assert stock_w1["lineage_diversity"] == 4

    paired = report["paired"]["promoted_per_100_wakes"]
    assert paired["mean_delta"] == 50.0  # stock - tuned, both worlds
    assert paired["n_worlds"] == 2
    low, high = paired["ci95"]
    assert low > 0  # identical deltas -> degenerate CI above zero

    verdict = ab_verdict(report)
    assert verdict["metric"] == "promoted_per_100_wakes"
    assert verdict["winner"] == "stock"
    assert verdict["ships"] is True

    assert (tmp_path / "campaign" / "report.json").exists()


def test_ab_verdict_inconclusive_on_straddling_ci(tmp_path) -> None:
    manifest = _campaign(tmp_path)
    # Arms split the worlds: stock wins w1, tuned wins w2 -> CI straddles 0.
    for pair in manifest["pairs"]:
        wins = (pair["arm"] == "stock") == (pair["world_id"] == "w1")
        store = JobStore(repo_root=Path(pair["sandbox"]))
        root = store.job_dir(pair["job_id"])
        proposals_dir = root / "proposals"
        proposals_dir.mkdir(exist_ok=True)
        if wins:
            (proposals_dir / "p0.json").write_text(
                json.dumps({"proposal_id": "p0", "application": {"status": "applied"}})
            )

    report = score_paired_campaign(manifest, run_records=[])
    verdict = ab_verdict(report)
    assert verdict["winner"] is None
    assert verdict["ships"] is False
