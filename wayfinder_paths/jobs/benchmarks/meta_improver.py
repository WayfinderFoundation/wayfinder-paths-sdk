"""Improver A/B harness (WOB L3): hold the benchmark worlds fixed, vary the
HARNESS. Two improver variants — prompt contract, gate configuration,
exploration allocation, archive policy — run through the production agent
lane on the SAME sealed worlds with matched model, wake count, and timeout.

Scored on descendant productivity, not one-shot utility: what each harness
BREEDS over generations — promotions per 100 wakes, false promotions
(promoted then judged hurt), utility per 1k output tokens, lineage
diversity, and (when oracle answers exist) true utility and regret of the
selected genome. The verdict is a paired per-world delta with a bootstrap
CI; an improver change whose CI straddles zero does not ship.

Run manually (never CI): model spend is real.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from wayfinder_paths.jobs.benchmarks.agent_adapter import (
    DEFAULT_AGENT,
    DEFAULT_OPENCODE,
    DEFAULT_SESSION_DB,
    build_world_bundle,
    harvest_lineage,
    meter_sessions,
    run_agent_wakes,
)
from wayfinder_paths.jobs.benchmarks.metrics import score_run
from wayfinder_paths.jobs.constitution import CONSTITUTION_FILENAME
from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.store import JobStore

MANIFEST_FILE = "manifest.json"
RUNS_FILE = "runs.jsonl"
REPORT_FILE = "report.json"
_GENERATION_CHECKPOINTS = (1, 3, 5)
_BOOTSTRAP_ITERATIONS = 2000
_BOOTSTRAP_SEED = 7


@dataclass(frozen=True)
class ImproverVariant:
    """One harness configuration under test. Every field is an override on
    top of the stock bundle; a variant with no overrides IS the stock
    harness (the natural control arm)."""

    name: str
    description: str = ""
    # Full markdown replacing the worker agent definition (the prompt
    # contract) inside the sandbox's .opencode/agents/.
    agent_definition: str | None = None
    # Deep-merged into the job's constitution.yaml (gate configuration).
    constitution_overrides: Mapping[str, Any] | None = None
    # Deep-merged into job.yaml execution_params (exploration allocation,
    # archive policy knobs — whatever the harness reads from params).
    execution_param_overrides: Mapping[str, Any] | None = None

    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "name": self.name,
                "agent_definition": self.agent_definition,
                "constitution_overrides": self.constitution_overrides,
                "execution_param_overrides": self.execution_param_overrides,
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def build_paired_campaign(
    worlds: list[Any],
    variant_a: ImproverVariant,
    variant_b: ImproverVariant,
    *,
    campaign_dir: Path,
    repo_root: Path,
    initial_genomes: Mapping[str, Any],
    wakes: int,
    model: str,
    timeout_s: int = 1800,
    bundle_builder: Callable[..., str] = build_world_bundle,
) -> dict[str, Any]:
    """One sandbox per (arm, world), both arms seeded with the SAME initial
    genome per world — the pairing is the design; only the harness differs.
    The manifest commits the arm fingerprints and matched budget BEFORE any
    wake runs (same commit-then-run discipline as the sealed worlds)."""
    if variant_a.name == variant_b.name:
        raise ValueError("arms must have distinct names")

    pairs: list[dict[str, Any]] = []
    for variant in (variant_a, variant_b):
        for world in worlds:
            genome = initial_genomes[world.world_id]
            sandbox = campaign_dir / "arms" / variant.name / world.world_id
            job_id = bundle_builder(
                world, sandbox=sandbox, repo_root=repo_root, initial_genome=genome
            )
            _apply_variant(sandbox, job_id, variant)
            pairs.append(
                {
                    "world_id": world.world_id,
                    "arm": variant.name,
                    "sandbox": str(sandbox),
                    "job_id": job_id,
                }
            )

    campaign_id = hashlib.sha256(
        json.dumps(
            {
                "worlds": sorted(world.world_id for world in worlds),
                "arms": [variant_a.fingerprint(), variant_b.fingerprint()],
                "budget": {"wakes": wakes, "model": model, "timeout_s": timeout_s},
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()[:16]
    manifest = {
        "campaign_id": campaign_id,
        "campaign_dir": str(campaign_dir),
        "arm_order": [variant_a.name, variant_b.name],
        "arms": {
            variant.name: {
                "fingerprint": variant.fingerprint(),
                "description": variant.description,
            }
            for variant in (variant_a, variant_b)
        },
        "budget": {"wakes": wakes, "model": model, "timeout_s": timeout_s},
        "worlds": [world.world_id for world in worlds],
        "pairs": pairs,
        "created_at": utc_now_iso(),
    }
    campaign_dir.mkdir(parents=True, exist_ok=True)
    (campaign_dir / MANIFEST_FILE).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def _apply_variant(
    sandbox: Path, job_id: str, variant: ImproverVariant, *, agent: str = DEFAULT_AGENT
) -> None:
    store = JobStore(repo_root=sandbox)
    root = store.job_dir(job_id)

    if variant.agent_definition is not None:
        agents_dir = sandbox / ".opencode" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / f"{agent}.md").write_text(
            variant.agent_definition, encoding="utf-8"
        )

    if variant.constitution_overrides:
        path = root / CONSTITUTION_FILENAME
        base = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
        merged = _deep_merge(base or {}, dict(variant.constitution_overrides))
        path.write_text(yaml.safe_dump(merged, sort_keys=False), encoding="utf-8")

    if variant.execution_param_overrides:
        job_yaml_path = root / "job.yaml"
        job_yaml = yaml.safe_load(job_yaml_path.read_text(encoding="utf-8")) or {}
        job_yaml["execution_params"] = _deep_merge(
            dict(job_yaml.get("execution_params") or {}),
            dict(variant.execution_param_overrides),
        )
        job_yaml_path.write_text(
            yaml.safe_dump(job_yaml, sort_keys=False), encoding="utf-8"
        )

    store.write_json(
        job_id,
        "meta_variant.json",
        {"name": variant.name, "fingerprint": variant.fingerprint()},
    )


def _deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overrides.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(dict(merged[key]), dict(value))
        else:
            merged[key] = value
    return merged


def run_paired_campaign(
    manifest: Mapping[str, Any],
    *,
    opencode: Path = DEFAULT_OPENCODE,
    session_db: Path = DEFAULT_SESSION_DB,
    wake_runner: Callable[..., list[dict[str, Any]]] = run_agent_wakes,
    meter: Callable[..., dict[str, Any]] = meter_sessions,
) -> list[dict[str, Any]]:
    """Drive every (arm, world) through the agent lane, arm-interleaved per
    world so slow drift in model behavior lands on both arms evenly. Appends
    each record to runs.jsonl as it completes — a killed campaign keeps its
    finished pairs."""
    budget = manifest["budget"]
    arm_rank = {name: i for i, name in enumerate(manifest["arm_order"])}
    ordered = sorted(
        manifest["pairs"], key=lambda p: (p["world_id"], arm_rank[p["arm"]])
    )
    runs_path = Path(manifest["campaign_dir"]) / RUNS_FILE
    records: list[dict[str, Any]] = []
    for pair in ordered:
        sessions = wake_runner(
            sandbox=Path(pair["sandbox"]),
            job_id=pair["job_id"],
            wakes=int(budget["wakes"]),
            model=str(budget["model"]),
            opencode=opencode,
            timeout_s=int(budget["timeout_s"]),
        )
        tokens = meter(sessions, session_db=session_db)
        record = {**pair, "sessions": sessions, "tokens": tokens}
        records.append(record)
        with runs_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
    return records


def score_paired_campaign(
    manifest: Mapping[str, Any],
    *,
    oracles: Mapping[str, Mapping[str, Any]] | None = None,
    run_records: list[dict[str, Any]] | None = None,
    bootstrap_seed: int = _BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """Descendant-productivity scoring per pair, then paired per-world deltas
    (arm A − arm B) with a world-resampled bootstrap CI per metric."""
    if run_records is None:
        run_records = _read_runs(Path(manifest["campaign_dir"]) / RUNS_FILE)
    tokens_by_pair = {
        (r["world_id"], r["arm"]): (r.get("tokens") or {}) for r in run_records
    }

    wakes = int(manifest["budget"]["wakes"])
    per_pair: list[dict[str, Any]] = []
    for pair in manifest["pairs"]:
        tokens = tokens_by_pair.get((pair["world_id"], pair["arm"]), {})
        metrics = _pair_metrics(
            Path(pair["sandbox"]),
            pair["job_id"],
            wakes=wakes,
            tokens_out=int(tokens.get("tokens_out") or 0),
        )
        if oracles and pair["world_id"] in oracles:
            harvest = harvest_lineage(
                sandbox=Path(pair["sandbox"]), job_id=pair["job_id"]
            )
            score = score_run(harvest, dict(oracles[pair["world_id"]]))
            metrics.update(
                {
                    "selected_utility": score["selected_utility"],
                    "search_regret_norm": score["search_regret_norm"],
                    "selection_regret_norm": score["selection_regret_norm"],
                    "epsilon_hit_selected": int(score["epsilon_hit_selected"]),
                }
            )
        per_pair.append({"world_id": pair["world_id"], "arm": pair["arm"], **metrics})

    arm_a, arm_b = manifest["arm_order"]
    by_key = {(row["world_id"], row["arm"]): row for row in per_pair}
    metric_names = sorted(
        {
            key
            for row in per_pair
            for key, value in row.items()
            if key not in ("world_id", "arm") and isinstance(value, (int, float))
        }
    )
    paired: dict[str, Any] = {}
    for metric in metric_names:
        deltas = []
        for world_id in manifest["worlds"]:
            row_a = by_key.get((world_id, arm_a))
            row_b = by_key.get((world_id, arm_b))
            if row_a is None or row_b is None:
                continue
            value_a, value_b = row_a.get(metric), row_b.get(metric)
            if value_a is None or value_b is None:
                continue
            deltas.append(float(value_a) - float(value_b))
        if not deltas:
            continue
        low, high = _paired_bootstrap_ci(deltas, seed=bootstrap_seed)
        paired[metric] = {
            "mean_delta": round(sum(deltas) / len(deltas), 6),
            "ci95": [round(low, 6), round(high, 6)],
            "n_worlds": len(deltas),
        }

    report = {
        "campaign_id": manifest["campaign_id"],
        "arm_order": [arm_a, arm_b],
        "per_pair": per_pair,
        # Positive mean_delta = arm A ahead on that metric; the CI decides.
        "paired": paired,
        "generated_at": utc_now_iso(),
    }
    campaign_dir = Path(manifest["campaign_dir"])
    if campaign_dir.is_dir():
        (campaign_dir / REPORT_FILE).write_text(
            json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
        )
    return report


def ab_verdict(
    report: Mapping[str, Any],
    *,
    primary: tuple[str, ...] = ("selected_utility", "promoted_per_100_wakes"),
) -> dict[str, Any]:
    """Ship gate for improver changes: the first primary metric present
    decides. Winner only when the paired CI excludes zero — otherwise the
    change does not ship."""
    arm_a, arm_b = report["arm_order"]
    for metric in primary:
        entry = (report.get("paired") or {}).get(metric)
        if not entry:
            continue
        low, high = entry["ci95"]
        winner = arm_a if low > 0 else arm_b if high < 0 else None
        return {
            "metric": metric,
            "winner": winner,
            "mean_delta": entry["mean_delta"],
            "ci95": entry["ci95"],
            "n_worlds": entry["n_worlds"],
            "ships": winner is not None,
        }
    return {"metric": None, "winner": None, "ships": False}


def _pair_metrics(
    sandbox: Path, job_id: str, *, wakes: int, tokens_out: int
) -> dict[str, Any]:
    """Descendant productivity from the job dir the harness left behind —
    proposals, verdicts, archive, and trial lineage are the phenotype of the
    harness, independent of any oracle."""
    store = JobStore(repo_root=sandbox)
    root = store.job_dir(job_id)

    proposals = []
    for path in sorted((root / "proposals").glob("*.json")):
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if isinstance(loaded, dict):
            proposals.append(loaded)
    promoted = [
        p for p in proposals if (p.get("application") or {}).get("status") == "applied"
    ]

    verdicts = store.read_json(job_id, "state/promotion_verdicts.json") or {}
    verdict_of = [str(v.get("verdict") or "") for v in verdicts.values()]

    archive = store.read_json(job_id, "state/archive.json") or {}
    growths = [
        float(entry["objective"].get("net_log_growth") or 0.0)
        for entry in sorted(
            (
                e
                for e in archive.get("candidates") or []
                if isinstance(e.get("objective"), dict)
            ),
            key=lambda e: str(e.get("created_at") or ""),
        )
    ]
    generation_utility = {
        f"utility_after_gen_{n}": (max(growths[:n]) if len(growths) >= n else None)
        for n in _GENERATION_CHECKPOINTS
    }
    utility_gain = (max(growths) - growths[0]) if len(growths) > 1 else 0.0

    trials = _read_runs(root / "results" / "backtest" / "trials.jsonl")
    distinct_params = len(
        {json.dumps(t.get("params"), sort_keys=True, default=str) for t in trials}
    )
    behavior_returns = [
        float((t.get("behavior") or {}).get("net_return"))
        for t in trials
        if isinstance((t.get("behavior") or {}).get("net_return"), (int, float))
    ]

    return {
        "proposals": len(proposals),
        "promoted": len(promoted),
        "promoted_per_100_wakes": (
            round(len(promoted) / wakes * 100, 4) if wakes else None
        ),
        "false_promotions": verdict_of.count("hurt"),
        "beats": verdict_of.count("beat"),
        "trials": len(trials),
        "lineage_diversity": distinct_params,
        "behavior_spread": (
            round(_std(behavior_returns), 6) if len(behavior_returns) > 1 else 0.0
        ),
        **generation_utility,
        "utility_gain": round(utility_gain, 6),
        "tokens_out": tokens_out,
        "utility_per_1k_tokens": (
            round(utility_gain / tokens_out * 1000, 6) if tokens_out else None
        ),
    }


def _paired_bootstrap_ci(
    deltas: list[float],
    *,
    seed: int,
    iterations: int = _BOOTSTRAP_ITERATIONS,
) -> tuple[float, float]:
    rng = random.Random(seed)
    means = sorted(
        sum(rng.choice(deltas) for _ in deltas) / len(deltas) for _ in range(iterations)
    )
    return means[int(0.025 * iterations)], means[int(0.975 * iterations) - 1]


def _std(values: list[float]) -> float:
    mean = sum(values) / len(values)
    return (sum((v - mean) ** 2 for v in values) / (len(values) - 1)) ** 0.5


def _read_runs(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows
