"""Remove a job for good — delete its runner loops and archive its directory
with every sidecar it left around the repo (runner logs, monitor state,
compiled wrappers, governance). `restore_job` is the undo.

The archive lives beside `.wayfinder/jobs` because `JobStore.list_jobs()`
globs only `jobs/*/job.yaml`: a removed job vanishes from every listing and
sync without being deleted from disk."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.application import pause_job_loops
from wayfinder_paths.jobs.background import op_running
from wayfinder_paths.jobs.compiler import JobCompiler
from wayfinder_paths.jobs.compute_lock import machine_state_lock
from wayfinder_paths.jobs.governance import governance_dir
from wayfinder_paths.jobs.models import WayfinderJob, safe_job_id, utc_now_iso
from wayfinder_paths.jobs.runner_bridge import RunnerBridge
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.sync import sync_all_jobs
from wayfinder_paths.runner.paths import get_runner_paths

ARCHIVE_DIRNAME = "jobs_archived"
ARCHIVE_MANIFEST = "archived.json"
ARTIFACTS_SUBDIR = "removed_artifacts"

_LOCK_NAME = "jobs_remove"


def remove_job(
    store: JobStore, job_id: str, *, force: bool = False, by: str = "owner"
) -> dict[str, Any]:
    job = store.load(job_id)
    if not force:
        _refuse_while_capital_at_stake(store, job)
    _refuse_while_background_op_running(store, job.id)

    undo = {"command": f"wayfinder job restore {job.id}"}
    # Not job_state_lock: that lock lives under .wayfinder/jobs/<id>/state/
    # and would recreate the tree we are moving away.
    with machine_state_lock(store.repo_root, name=_LOCK_NAME):
        loops = _delete_runner_loops(store, job)
        sidecars = _sidecar_table(store, job)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        archive_dir = (
            store.jobs_dir.parent / ARCHIVE_DIRNAME / f"{safe_job_id(job.id)}.{stamp}"
        )
        archive_rel = str(archive_dir.relative_to(store.repo_root))
        moved = [
            {
                "original": _display_path(original, store.repo_root),
                "archived": str((archive_dir / relative).relative_to(store.repo_root)),
            }
            for original, relative in sidecars
        ]
        store.write_json(
            job.id,
            ARCHIVE_MANIFEST,
            {
                "job_id": job.id,
                "removed_at": utc_now_iso(),
                "by": by,
                "forced": bool(force),
                "archive_dir": archive_rel,
                "runner_responses": loops,
                "moved": moved,
                "undo": undo,
            },
        )
        store.append_journal(
            job.id,
            {
                "type": "job_removed",
                "by": by,
                "forced": bool(force),
                "archive_dir": archive_rel,
                "runner_responses": loops,
                "undo": undo,
            },
        )
        # Last store writes for this id above: save/refresh_scorecard/
        # write_json/append_journal all mkdir the job tree back into
        # existence, so nothing may touch the store for this id after the move.
        archive_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(store.job_dir(job.id)), str(archive_dir))
        for original, relative in sidecars:
            target = archive_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(original), str(target))

    sync_all_jobs(store=store)
    return {
        "job_id": job.id,
        "removed": True,
        "archive_dir": archive_rel,
        "forced": bool(force),
        "loops": loops,
        "moved": moved,
        "undo": undo,
    }


def restore_job(
    store: JobStore,
    job_id: str,
    *,
    archive_dir: Path | None = None,
    by: str = "owner",
) -> dict[str, Any]:
    job_id = safe_job_id(job_id)
    with machine_state_lock(store.repo_root, name=_LOCK_NAME):
        if store.job_yaml_path(job_id).exists():
            raise ValueError("cannot restore: a job with this id already exists")
        source = (
            store.repo_root / archive_dir
            if archive_dir is not None
            else _latest_archive(store, job_id)
        )
        manifest = json.loads((source / ARCHIVE_MANIFEST).read_text(encoding="utf-8"))
        if manifest["job_id"] != job_id:
            raise ValueError(
                f"cannot restore: {_display_path(source, store.repo_root)} archives "
                f"{manifest['job_id']}, not {job_id}"
            )
        source_rel = _display_path(source, store.repo_root)
        for entry in reversed(manifest["moved"]):
            original = store.repo_root / entry["original"]
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(store.repo_root / entry["archived"]), str(original))
        if (source / ARTIFACTS_SUBDIR).exists():
            # Only the now-empty scaffold the sidecars were parked under.
            shutil.rmtree(source / ARTIFACTS_SUBDIR)
        store.jobs_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(store.job_dir(job_id)))
        (store.job_dir(job_id) / ARCHIVE_MANIFEST).unlink()
        job = store.load(job_id)
        compile_result = JobCompiler(store=store).compile(job)
        loops = pause_job_loops(store, job_id)
        store.append_journal(
            job_id,
            {
                "type": "job_restored",
                "by": by,
                "from": source_rel,
                "undo": {"command": f"wayfinder job remove {job_id}"},
            },
        )
    sync_all_jobs(store=store)
    return {
        "job_id": job_id,
        "restored": True,
        "from": source_rel,
        "compile": compile_result,
        "loops": loops,
    }


def _refuse_while_capital_at_stake(store: JobStore, job: WayfinderJob) -> None:
    if job.script_loop.mode == "live":
        raise ValueError(
            f"cannot remove: {job.id} is live — go paper first (set_script_mode "
            "paper), withdraw the bankroll, then remove; or pass --force to "
            "orphan venue positions deliberately"
        )
    funding = store.read_json(job.id, "state/funding.json") or {}
    capital = float(job.execution_params.get("initial_capital") or 0)
    if funding.get("venue_funded") and capital > 0:
        raise ValueError(
            f"cannot remove: {job.id} holds venue capital (${capital:g} declared) "
            "— withdraw the bankroll first so no venue money sits unmanaged, or "
            "pass --force"
        )
    engine = store.read_json(job.id, "state/engine_state.json") or {}
    open_positions = {
        symbol: position
        for symbol, position in (engine.get("positions") or {}).items()
        if position
    }
    if str(engine.get("mode")) == "live" and open_positions:
        raise ValueError(
            "cannot remove: the live engine holds open positions "
            f"({', '.join(sorted(open_positions))}) — flatten first "
            "(wayfinder job halt --flatten), or pass --force"
        )


def _refuse_while_background_op_running(store: JobStore, job_id: str) -> None:
    job_dir = store.job_dir(job_id)
    for status_path in sorted((job_dir / "state" / "background_ops").glob("*.json")):
        if status_path.name.endswith(".result.json"):
            continue
        if op_running(job_dir, status_path.stem):
            raise ValueError(
                "cannot remove: a background operation is still running "
                f"({status_path.stem}) — wait for it (op_status) or cancel it "
                '(core_jobs(action="op_cancel")) and retry'
            )
        if _op_queued(status_path):
            raise ValueError(
                "cannot remove: a background operation is still queued "
                f"({status_path.stem}) in the heavy lane — cancel it "
                '(core_jobs(action="op_cancel")) or wait for it (op_status) and retry'
            )


def _op_queued(status_path: Path) -> bool:
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(status, dict) and status.get("state") == "queued"


def _delete_runner_loops(store: JobStore, job: WayfinderJob) -> list[dict[str, Any]]:
    bridge = RunnerBridge(repo_root=store.repo_root)
    responses: list[dict[str, Any]] = []
    for loop_name, loop in (("script", job.script_loop), ("agent", job.agent_loop)):
        if not loop.runner_job_name:
            continue
        response = bridge.delete(loop.runner_job_name)
        error = str(response.get("error") or "")
        if not response.get("ok") and not error.startswith("Job not found"):
            raise ValueError(
                f"cannot remove: runner refused to delete loop "
                f"{loop.runner_job_name}: {error} — retry when the run finishes"
            )
        responses.append(
            {
                "loop": loop_name,
                "runner_job_name": loop.runner_job_name,
                "response": response,
            }
        )
    return responses


def _sidecar_table(store: JobStore, job: WayfinderJob) -> list[tuple[Path, str]]:
    runner_paths = get_runner_paths(repo_root=store.repo_root)
    safe_id = safe_job_id(job.id)
    module_name = job.id.replace("-", "_")
    candidates: list[tuple[Path, str]] = []
    for loop_name, loop in (("script", job.script_loop), ("agent", job.agent_loop)):
        if loop.runner_job_name:
            candidates.append(
                (
                    runner_paths.logs_dir / loop.runner_job_name,
                    f"{ARTIFACTS_SUBDIR}/runner_logs/{loop_name}",
                )
            )
    for namespace in (f"{safe_id}-script", f"{safe_id}-agent", f"{safe_id}-dryrun"):
        candidates.append(
            (
                runner_paths.runner_dir / "job_state" / namespace,
                f"{ARTIFACTS_SUBDIR}/runner_state/{namespace}",
            )
        )
    for wrapper in (f"{module_name}_script.py", f"{module_name}_agent.py"):
        candidates.append(
            (store.runs_jobs_dir / wrapper, f"{ARTIFACTS_SUBDIR}/wrappers/{wrapper}")
        )
    candidates.append(
        (governance_dir(store.repo_root, job.id), f"{ARTIFACTS_SUBDIR}/governance")
    )
    return [
        (original, relative) for original, relative in candidates if original.exists()
    ]


def _latest_archive(store: JobStore, job_id: str) -> Path:
    archives = sorted((store.jobs_dir.parent / ARCHIVE_DIRNAME).glob(f"{job_id}.*"))
    if not archives:
        raise ValueError(
            f"cannot restore: no archive of {job_id} under .wayfinder/{ARCHIVE_DIRNAME}/"
        )
    return archives[-1]


def _display_path(path: Path, repo_root: Path) -> str:
    # The runner dir can be pointed outside the repo (WAYFINDER_RUNNER_DIR);
    # those sidecars are recorded absolute so restore still finds them.
    return (
        str(path.relative_to(repo_root))
        if path.is_relative_to(repo_root)
        else str(path)
    )
