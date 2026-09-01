"""Frozen benchmark worlds with a physically sealed chronological holdout."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.bench.env import atomic_json, sha256_file, sha256_json
from wayfinder_paths.jobs.bundles import copy_job_bundle
from wayfinder_paths.jobs.execution.features import parse_feature_specs
from wayfinder_paths.jobs.execution.job import _load_dataset, _load_job_yaml
from wayfinder_paths.jobs.execution.primitives import ExecutionSpec
from wayfinder_paths.jobs.execution.validation import resolve_execution_spec
from wayfinder_paths.jobs.gating import compute_workspace_revision

WORLD_SCHEMA_VERSION = "1.0"


def prepare_world(
    source_job: Path,
    world_dir: Path,
    *,
    generation_cutoff: datetime,
    holdout_end: datetime,
    sealed_dir: Path,
    world_id: str | None = None,
) -> dict[str, Any]:
    """Freeze one real job into an agent-visible prefix and owner-only tail."""
    source_job = source_job.resolve()
    world_dir = world_dir.resolve()
    sealed_dir = sealed_dir.resolve()
    if world_dir.exists() and any(world_dir.iterdir()):
        raise FileExistsError(f"world directory is not empty: {world_dir}")
    if sealed_dir.exists() and any(sealed_dir.iterdir()):
        raise FileExistsError(f"sealed directory is not empty: {sealed_dir}")
    cutoff = _aware(generation_cutoff)
    end = _aware(holdout_end)
    if end <= cutoff:
        raise ValueError("holdout_end must be after generation_cutoff")
    holdout_days = (end - cutoff).total_seconds() / 86_400
    if not 14 <= holdout_days <= 21:
        raise ValueError("benchmark holdout must span 14 to 21 days")
    if (
        world_dir == sealed_dir
        or world_dir.is_relative_to(sealed_dir)
        or sealed_dir.is_relative_to(world_dir)
    ):
        raise ValueError("sealed holdout must be physically outside the world")

    job_data = _load_job_yaml(source_job)
    spec_data, _ = resolve_execution_spec(source_job, job_data)
    if not spec_data:
        raise FileNotFoundError("source job has no execution_spec")
    spec = ExecutionSpec.from_dict(spec_data)
    execution_params = dict(job_data.get("execution_params") or {})
    dataset = _load_dataset(
        source_job,
        spec,
        job_data,
        feature_roots=(source_job,),
        include_store_features=True,
    )
    rows = dataset.bars.to_rows()
    development = [row for row in rows if _row_timestamp(row) <= cutoff]
    holdout = [row for row in rows if cutoff < _row_timestamp(row) <= end]
    if not development or not holdout:
        raise ValueError("world needs non-empty development and holdout bar sets")

    resolved_world_id = world_id or (
        f"{source_job.name}-{cutoff.strftime('%Y%m%dT%H%M%SZ')}"
    )
    world_dir.mkdir(parents=True, exist_ok=True)
    sealed_dir.mkdir(parents=True, exist_ok=True)
    incumbent = world_dir / "incumbent"
    copy_job_bundle(source_job, incumbent)
    development_payload = {
        "bars": development,
        "metadata": {
            **dict(dataset.metadata),
            "world_id": resolved_world_id,
            "partition": "development",
            "generation_cutoff": cutoff.isoformat(),
        },
    }
    holdout_payload = {
        "bars": holdout,
        "metadata": {
            **dict(dataset.metadata),
            "world_id": resolved_world_id,
            "partition": "holdout",
            "generation_cutoff": cutoff.isoformat(),
            "holdout_end": end.isoformat(),
        },
    }
    atomic_json(world_dir / "bars.json", development_payload)
    atomic_json(sealed_dir / "holdout.json", holdout_payload)
    features = _freeze_feature_prefixes(
        source_job,
        world_dir=world_dir,
        cutoff=cutoff,
        spec=spec,
    )

    source_bars = source_job / "results" / "backtest" / "input_bars.json"
    manifest = {
        "schema_version": WORLD_SCHEMA_VERSION,
        "world_id": resolved_world_id,
        "created_at": datetime.now(UTC).isoformat(),
        "source_job": source_job.name,
        "source_revision": compute_workspace_revision(source_job),
        "generation_cutoff": cutoff.isoformat(),
        "holdout_end": end.isoformat(),
        "development_bars": len(development),
        "holdout_bars": len(holdout),
        "dataset": {
            "source_bytes_sha256": sha256_file(source_bars),
            "full_rows_sha256": sha256_json(rows),
            "benchmark_rows_sha256": sha256_json([*development, *holdout]),
            "development_sha256": sha256_json(development_payload),
            "holdout_commitment": sha256_json(holdout_payload),
        },
        "execution_environment": {
            "spec_sha256": sha256_json(spec.to_dict()),
            "data_contract": dict(spec.data_contract),
            "params": _frozen_execution_params(execution_params, spec),
        },
        "incumbent": {
            "path": "incumbent",
            "revision": compute_workspace_revision(incumbent),
        },
        "features": features,
        # The runner receives sealed_dir separately. No owner path or holdout
        # bytes are written into the agent-visible world.
        "holdout_locator": "owner-supplied",
    }
    atomic_json(world_dir / "world.json", manifest)
    return manifest


def load_world(world_dir: Path, sealed_dir: Path) -> dict[str, Any]:
    manifest = json.loads((world_dir / "world.json").read_text(encoding="utf-8"))
    development = json.loads((world_dir / "bars.json").read_text(encoding="utf-8"))
    holdout = json.loads((sealed_dir / "holdout.json").read_text(encoding="utf-8"))
    if sha256_json(development) != manifest["dataset"]["development_sha256"]:
        raise ValueError("development world bytes changed after freeze")
    if sha256_json(holdout) != manifest["dataset"]["holdout_commitment"]:
        raise ValueError("sealed holdout bytes do not match the commitment")
    return {"manifest": manifest, "development": development, "holdout": holdout}


def install_development_world(
    world_dir: Path,
    *,
    destination_job: Path,
) -> None:
    payload = (world_dir / "bars.json").read_bytes()
    target = destination_job / "results" / "backtest" / "input_bars.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    manifest = json.loads((world_dir / "world.json").read_text(encoding="utf-8"))
    for feature in manifest.get("features") or []:
        source = world_dir / str(feature["path"])
        if sha256_file(source) != feature["sha256"]:
            raise ValueError("development feature bytes changed after freeze")
        relative = Path(str(feature["target_path"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("feature target must remain inside the job root")
        destination = destination_job / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())


def _row_timestamp(row: dict[str, Any]) -> datetime:
    raw = row.get("timestamp", row.get("t"))
    stamp = pd.Timestamp(raw)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    return stamp.tz_convert("UTC").to_pydatetime()


def _freeze_feature_prefixes(
    source_job: Path,
    *,
    world_dir: Path,
    cutoff: datetime,
    spec: ExecutionSpec,
) -> list[dict[str, Any]]:
    frozen: list[dict[str, Any]] = []
    seen: set[str] = set()
    for feature in parse_feature_specs(spec):
        if feature.path in seen:
            continue
        seen.add(feature.path)
        relative = Path(feature.path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("benchmark feature paths must stay inside the job root")
        source = source_job / relative
        if not source.exists():
            raise FileNotFoundError(f"declared feature file is missing: {source}")
        lines: list[str] = []
        with source.open(encoding="utf-8") as handle:
            for raw in handle:
                try:
                    row = json.loads(raw)
                    timestamp = _row_timestamp(row)
                except (TypeError, ValueError):
                    continue
                if timestamp <= cutoff:
                    lines.append(json.dumps(row, sort_keys=True, default=str))
        target = world_dir / "features" / f"feature-{len(frozen):02d}.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        frozen.append(
            {
                "target_path": feature.path,
                "path": str(target.relative_to(world_dir)),
                "sha256": sha256_file(target),
                "rows": len(lines),
            }
        )
    return frozen


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _frozen_execution_params(
    params: dict[str, Any], spec: ExecutionSpec
) -> dict[str, Any]:
    symbols = params.get("symbols") or spec.data_contract.get("symbols") or []
    return {
        "symbols": list(symbols),
        "venue": params.get("venue"),
        "initial_capital": float(params.get("initial_capital") or 10_000.0),
        # A missing estimate must not turn the holdout into a zero-cost world.
        "fee_bps": float(
            params["fee_bps"] if params.get("fee_bps") is not None else 7.0
        ),
        "maker_fee_bps": float(
            params["maker_fee_bps"] if params.get("maker_fee_bps") is not None else 1.5
        ),
        "slippage_bps": float(params.get("slippage_bps") or 0.0),
        "stop_market_slippage_bps": float(
            params.get("stop_market_slippage_bps") or 0.0
        ),
    }
