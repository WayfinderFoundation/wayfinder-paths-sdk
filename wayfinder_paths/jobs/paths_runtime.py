"""Installed Paths as jobs.

A ``path_v1`` job pins one installed Path version (slug, version, bundle
sha256, extracted-tree sha256) in job.yaml and runs one of its components on
the job's schedule. The Path runs from its install directory and is never
copied into the workspace: Path code is published third-party code and must
not be agent-edited in place. Every tick re-hashes the bundle and the tree
against the pin before executing anything.

Two component kinds:

- ``freestyle``: the component is a ``tick(ctx)`` module; the freestyle
  runtime runs it (paper-capable, full telemetry).
- ``exec`` (default): the component is run as a subprocess the way
  ``wayfinder path exec`` runs it. It reports through ``WAYFINDER_PATH_EVENT``
  lines on stdout; without a declared dry-run mode it has no paper mode and
  the job launches live only after the ``no_dry_run`` flag is acknowledged.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.compiler import JobCompiler
from wayfinder_paths.jobs.execution.job import _load_job_yaml
from wayfinder_paths.jobs.execution.validation import (
    report_from_checks,
)
from wayfinder_paths.jobs.forward import ForwardRecorder
from wayfinder_paths.jobs.freestyle.create import SCRIPT_JOB_TRIGGERS
from wayfinder_paths.jobs.freestyle.runtime import (
    JOB_RESULT_MARKER,
    run_freestyle_tick,
)
from wayfinder_paths.jobs.freestyle.validate import (
    DEFAULT_DRY_RUN_TIMEOUT_S,
    MAX_TIMEOUT_S,
    run_dry_run,
    static_checks,
)
from wayfinder_paths.jobs.gating import compute_workspace_revision
from wayfinder_paths.jobs.halt import read_halt
from wayfinder_paths.jobs.launch import hold_job
from wayfinder_paths.jobs.models import (
    WayfinderJob,
    normalize_agent_mode,
    safe_job_id,
    utc_now_iso,
)
from wayfinder_paths.jobs.notify_policy import default_notifications
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.sync import sync_all_jobs
from wayfinder_paths.jobs.triggers import fire_triggers
from wayfinder_paths.paths.cli import (
    _canonical_install_root,
    _installed_path_dir,
    _load_install_lock,
    _sha256_file,
    _state_dir_for_install_root,
)
from wayfinder_paths.paths.evaluator import PathEvalError, run_path_eval
from wayfinder_paths.paths.manifest import PathManifest, PathManifestError

PIN_PATH = "workspace/config/path.json"
PARAMS_PATH = "workspace/config/params.json"
UPGRADE_STATE_PATH = "state/path_upgrade.json"
PATH_STATE_DIR = "state/path"
PATH_EVENT_MARKER = "WAYFINDER_PATH_EVENT "
DEFAULT_INSTALL_DIR = ".wayfinder/paths"
DEFAULT_TIMEOUT_S = 300
TREE_EXCLUDED_NAMES = {"bundle.zip", "install-intent.json"}
RECORDABLE_EVENTS = {"order", "fill", "trade_close", "trade_open", "tick", "funding"}


class PathRefusal(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# install lookup + identity


def resolve_install(
    store: JobStore, *, install_dir: str | Path | None, slug: str
) -> tuple[dict[str, Any] | None, Path, Path]:
    """(lock entry, install base, lock path) for a slug."""
    raw = Path(install_dir or DEFAULT_INSTALL_DIR)
    if not raw.is_absolute():
        raw = store.repo_root / raw
    base = _canonical_install_root(raw)
    lock, lock_path = _load_install_lock(_state_dir_for_install_root(base))
    entry = (lock.get("paths") or {}).get(slug)
    return (entry if isinstance(entry, dict) else None), base, lock_path


def tree_sha256(path_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(Path(path_dir).rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(path_dir)
        if (
            relative.name in TREE_EXCLUDED_NAMES
            or "__pycache__" in relative.parts
            or path.suffix == ".pyc"
            or (relative.parts and relative.parts[0] == "dist")
        ):
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def bundle_sha256(path_dir: Path) -> str | None:
    bundle = Path(path_dir) / "bundle.zip"
    return _sha256_file(bundle) if bundle.is_file() else None


def component_kind(component: dict[str, Any], job_block: dict[str, Any]) -> str:
    kind = str(job_block.get("kind") or component.get("kind") or "").strip().lower()
    return "freestyle" if kind == "freestyle" else "exec"


# ---------------------------------------------------------------------------
# create


def create_from_path(
    slug: str,
    *,
    job_id: str | None = None,
    version: str | None = None,
    component: str | None = None,
    params: dict[str, Any] | None = None,
    interval_seconds: int | None = None,
    cron_expr: str | None = None,
    timezone: str = "UTC",
    timeout_seconds: int | None = None,
    install_dir: str | Path | None = None,
    agent_mode: str = "monitor",
    store: JobStore | None = None,
    compile_job: bool = True,
    initializer_session_id: str | None = None,
) -> dict[str, Any]:
    """Pin an installed Path version into a paused ``path_v1`` job."""
    store = store or JobStore()
    entry, base, lock_path = resolve_install(store, install_dir=install_dir, slug=slug)
    if entry is None:
        raise FileNotFoundError(
            f"{slug} is not installed (no entry in {lock_path}); run `wayfinder path install {slug}` first"
        )
    pinned_version = str(version or entry.get("version") or "").strip()
    if not pinned_version:
        raise ValueError(f"{slug}: the lock entry has no version and none was given")
    path_dir = _installed_path_dir(
        base=base, slug=slug, version=pinned_version, entry=entry
    )
    if not path_dir.is_dir():
        raise FileNotFoundError(f"{slug} {pinned_version} is not on disk at {path_dir}")
    manifest = PathManifest.load(path_dir / "wfpath.yaml")
    job_block = (
        dict(manifest.raw.get("job") or {})
        if isinstance(manifest.raw.get("job"), dict)
        else {}
    )
    resolved = manifest.resolve_component(
        component or job_block.get("component") or None
    )
    component_id = str(resolved.get("id") or "main")
    component_path = str(resolved.get("path") or "")
    kind = component_kind(resolved, job_block)
    schedule = dict(job_block.get("schedule") or {})
    interval = interval_seconds or schedule.get("interval_seconds")
    cron = cron_expr or schedule.get("cron_expr")
    if not interval and not cron:
        raise ValueError(
            "path jobs need interval_seconds or cron_expr (or a job.schedule block in wfpath.yaml)"
        )
    timeout = int(
        timeout_seconds or schedule.get("timeout_seconds") or DEFAULT_TIMEOUT_S
    )
    lock_sha = str(entry.get("bundle_sha256") or "")
    on_disk_sha = bundle_sha256(path_dir)
    if lock_sha and on_disk_sha and lock_sha.lower() != on_disk_sha.lower():
        raise ValueError(
            f"{slug} {pinned_version}: bundle.zip on disk ({on_disk_sha}) does not match the lock ({lock_sha})"
        )
    merged_params = {**dict(job_block.get("params") or {}), **dict(params or {})}
    pin = {
        "kind": "path",
        "slug": slug,
        "version": pinned_version,
        "component": component_id,
        "component_path": component_path,
        "component_kind": kind,
        "bundle_sha256": lock_sha or on_disk_sha,
        "tree_sha256": tree_sha256(path_dir),
        "install_dir": str(path_dir),
        "dry_run": str(job_block.get("dry_run") or "unsupported"),
        "params": merged_params,
    }
    jid = safe_job_id(job_id or slug)
    job = WayfinderJob.new(
        jid,
        name=manifest.name,
        goal=manifest.summary or f"Run the {manifest.name} path",
        script=PIN_PATH,
        interval_seconds=int(interval) if interval else None,
        cron_expr=str(cron) if cron else None,
        timezone=str(schedule.get("timezone") or timezone),
        timeout_seconds=timeout,
        agent_mode=normalize_agent_mode(agent_mode),
        execution_contract="path_v1",
        initializer_session_id=initializer_session_id,
        source=pin,
    )
    job.agent_loop.triggers = list(SCRIPT_JOB_TRIGGERS)
    job.reporting = {**job.reporting, "notify": default_notifications(job)}
    root = store.init_layout(job)
    store.write_json(jid, PIN_PATH, pin)
    store.write_json(jid, PARAMS_PATH, merged_params)
    risk = job_block.get("risk")
    if isinstance(risk, dict) and risk:
        store.write_json(jid, "workspace/risk_limits.json", dict(risk))
    store.save(job)
    result: dict[str, Any] = {
        "job": job.to_dict(),
        "job_yaml": str(root / "job.yaml"),
        "pin": pin,
        "paper_capable": kind == "freestyle" or pin["dry_run"] == "supported",
        "hint": (
            "run validate_job (pin, manifest, eval fixtures, dry run), read the "
            "launch_checklist, then launch in paper"
            if kind == "freestyle" or pin["dry_run"] == "supported"
            else "this component declares no dry-run mode: validate, acknowledge "
            "the no_dry_run risk flag, then launch live"
        ),
    }
    if compile_job:
        result["compile"] = JobCompiler(store=store).compile(job)
        result["hold"] = hold_job(job.id, store=store)
        sync_all_jobs(store=store)
    return result


# ---------------------------------------------------------------------------
# validate


def validate_path_job(
    job_id: str,
    *,
    candidate_dir: str | Path | None = None,
    store: JobStore | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    store = store or JobStore()
    root = Path(candidate_dir) if candidate_dir else store.job_dir(job_id)
    checks: list[dict[str, Any]] = []
    job_data: dict[str, Any] = {}
    try:
        job_data = _load_job_yaml(root)
        checks.append({"name": "job_yaml_parse", "passed": True})
    except Exception as exc:  # noqa: BLE001
        checks.append({"name": "job_yaml_parse", "passed": False, "error": str(exc)})
    checks.append(
        {
            "name": "execution_contract_path_v1",
            "passed": str(job_data.get("execution_contract")) == "path_v1",
        }
    )
    pin = dict(job_data.get("source") or {})
    required = (
        "slug",
        "version",
        "bundle_sha256",
        "tree_sha256",
        "install_dir",
        "component_path",
    )
    missing = [key for key in required if not pin.get(key)]
    checks.append(
        {"name": "source_declared", "passed": not missing, "missing": missing}
    )
    section: dict[str, Any] = {"pin": pin}
    if missing:
        return _finish(
            store,
            job_id,
            root,
            checks,
            section,
            candidate_dir=candidate_dir,
            paper_capable=False,
        )
    slug = str(pin["slug"])
    path_dir = Path(str(pin["install_dir"]))
    entry, _base, lock_path = resolve_install(
        store, install_dir=path_dir.parent.parent, slug=slug
    )
    lock_matches = bool(
        entry
        and str(entry.get("version")) == str(pin["version"])
        and str(entry.get("bundle_sha256") or "").lower()
        == str(pin["bundle_sha256"]).lower()
    )
    checks.append(
        {
            "name": "install_lock_matches",
            "passed": lock_matches,
            "blocking": False,
            "lock": str(lock_path),
            "lock_version": entry.get("version") if entry else None,
            "hint": "the lock moved (path update?); the job keeps its pinned version until re-created",
        }
    )
    on_disk = bundle_sha256(path_dir) if path_dir.is_dir() else None
    checks.append(
        {
            "name": "bundle_sha256_verified",
            "passed": path_dir.is_dir()
            and (
                on_disk is None or on_disk.lower() == str(pin["bundle_sha256"]).lower()
            ),
            "on_disk": on_disk,
            "pinned": pin["bundle_sha256"],
        }
    )
    tree_ok = path_dir.is_dir() and tree_sha256(path_dir) == str(pin["tree_sha256"])
    checks.append(
        {
            "name": "installed_tree_matches_pin",
            "passed": tree_ok,
            "hint": "a file under the installed path changed since the job was created",
        }
    )
    manifest: PathManifest | None = None
    try:
        manifest = PathManifest.load(path_dir / "wfpath.yaml")
        checks.append({"name": "manifest_loads", "passed": True})
    except (PathManifestError, OSError) as exc:
        checks.append({"name": "manifest_loads", "passed": False, "error": str(exc)})
    target = path_dir / str(pin["component_path"])
    checks.append(
        {"name": "component_exists", "passed": target.is_file(), "path": str(target)}
    )
    if manifest is not None:
        try:
            report = run_path_eval(path_dir=path_dir)
            checks.append(
                {
                    "name": "path_eval_passed",
                    "passed": report.ok,
                    "issues": [
                        {"name": i.name, "passed": i.passed, "message": i.message}
                        for i in report.issues
                        if not i.passed
                    ],
                }
            )
        except PathEvalError as exc:
            missing_evals = "tests/evals" in str(exc)
            checks.append(
                {
                    "name": "path_eval_passed",
                    "passed": missing_evals,
                    "blocking": False if missing_evals else True,
                    "skipped": missing_evals,
                    "error": str(exc),
                }
            )
    kind = str(pin.get("component_kind") or "exec")
    dry_run_declared = str(pin.get("dry_run") or "unsupported") == "supported"
    paper_capable = kind == "freestyle" or dry_run_declared
    timeout_s = int((job_data.get("script_loop") or {}).get("timeout_seconds") or 0)
    timeout = timeout_s if 0 < timeout_s <= MAX_TIMEOUT_S else DEFAULT_DRY_RUN_TIMEOUT_S
    section.update({"component_kind": kind, "dry_run_declared": pin.get("dry_run")})
    mechanically_ok = all(c["passed"] for c in checks if c.get("blocking") is not False)
    if target.is_file() and kind == "freestyle":
        checks.extend(static_checks(target))
        if dry_run and mechanically_ok:
            outcome = run_dry_run(
                root,
                job_id=job_id,
                repo_root=store.repo_root,
                timeout_s=timeout,
                entrypoint=target,
                extra_sys_path=[str(path_dir)],
            )
            result = outcome.get("result") or {}
            checks.append(
                {
                    "name": "dry_run_ok",
                    "passed": bool(outcome.get("ok")),
                    "error": outcome.get("error"),
                    "ticks": outcome.get("ticks"),
                }
            )
            section["freestyle"] = {
                "spec": result.get("spec") or {},
                "dry_run": {
                    "ok": outcome.get("ok"),
                    "intents": [
                        a.get("intent")
                        for a in result.get("actions") or []
                        if a.get("intent")
                    ],
                    "actions": result.get("actions") or [],
                    "unpapered_actions": result.get("unpapered_actions") or [],
                },
            }
    elif target.is_file() and dry_run and mechanically_ok:
        if dry_run_declared:
            outcome = _exec_component(
                root,
                job_data,
                pin,
                path_dir,
                mode="paper",
                dry_run=True,
                timeout=timeout,
            )
            checks.append(
                {
                    "name": "dry_run_exec",
                    "passed": bool(outcome.get("ok")),
                    "exit_code": outcome.get("exit_code"),
                    "timed_out": bool(outcome.get("timed_out")),
                    "error": outcome.get("error"),
                    "events": len(outcome.get("events") or []),
                }
            )
            section["dry_run"] = outcome
        else:
            checks.append(
                {
                    "name": "dry_run_exec",
                    "passed": True,
                    "skipped": True,
                    "reason": (
                        "the component declares no dry-run mode (wfpath.yaml job.dry_run); "
                        "it was not executed and has no paper mode"
                    ),
                }
            )
    return _finish(
        store,
        job_id,
        root,
        checks,
        section,
        candidate_dir=candidate_dir,
        paper_capable=paper_capable,
    )


def _finish(
    store: JobStore,
    job_id: str,
    root: Path,
    checks: list[dict[str, Any]],
    section: dict[str, Any],
    *,
    candidate_dir: str | Path | None,
    paper_capable: bool,
) -> dict[str, Any]:
    report = report_from_checks(checks, strict=False)
    report["revision"] = compute_workspace_revision(root)
    report["kind"] = "path_v1"
    report["paper_capable"] = paper_capable
    report["path"] = section
    if section.get("freestyle"):
        report["freestyle"] = section["freestyle"]
    if not candidate_dir:
        store.write_json(job_id, "reports/validation/latest.json", report)
    return report


# ---------------------------------------------------------------------------
# tick


def run_path_tick(job_dir: str | Path | None = None) -> dict[str, Any]:
    root = Path(job_dir or os.environ["WAYFINDER_JOB_DIR"])
    mode = os.environ.get("WAYFINDER_JOB_MODE") or "paper"
    store: JobStore | None = None
    job: WayfinderJob | None = None
    payload: dict[str, Any]
    try:
        job_data = _load_job_yaml(root)
        job = WayfinderJob.from_dict(job_data)
        if job.execution_contract != "path_v1":
            raise PathRefusal(
                f"job {job.id} is on the {job.execution_contract} contract, not path_v1"
            )
        declared_mode = str(job.script_loop.mode or "paper")
        divergence = None
        if mode == "live" and declared_mode != "live":
            divergence = {
                "kind": "mode_divergence",
                "runner_mode": "live",
                "declared_mode": declared_mode,
                "action": "downgraded_to_paper",
            }
            mode = "paper"
        pinned = os.environ.get("WAYFINDER_JOB_REVISION") or ""
        current = compute_workspace_revision(root)
        if pinned and pinned != current:
            raise PathRefusal(
                f"revision drift: launched at {pinned}, workspace is {current}; re-run validate and launch"
            )
        store = JobStore()
        pin = dict(job.source or {})
        path_dir = Path(str(pin.get("install_dir") or ""))
        mismatch = _pin_mismatch(pin, path_dir)
        recorder = ForwardRecorder(
            job_id=job.id, job_dir=root, mode=mode, revision=current or None
        )
        if mismatch:
            recorder.record_run(
                status="failed",
                decision={"action": "path_component", "reason": mismatch},
                mode=mode,
                guard_events=[{"kind": "path_pin_mismatch", "reason": mismatch}],
            )
            store.append_journal(
                job.id, {"type": "path_pin_mismatch", "reason": mismatch[:300]}
            )
            raise PathRefusal(mismatch)
        _note_upgrade(store, job, pin)
        halt = read_halt(root)
        if halt:
            recorder.record_run(
                status="halted",
                decision={"action": "path_component", "reason": "halted"},
                mode=mode,
                guard_events=[{"kind": "manual_halt", "reason": halt.get("reason")}],
            )
            payload = {
                "ok": True,
                "status": "halted",
                "summary": f"halted: {halt.get('reason')}",
                "guard_events": [{"kind": "manual_halt", "reason": halt.get("reason")}],
            }
        elif str(pin.get("component_kind") or "exec") == "freestyle":
            target = path_dir / str(pin.get("component_path") or "")
            return run_freestyle_tick(
                root, entrypoint=target, extra_sys_path=[str(path_dir)]
            )
        elif (
            mode == "paper" and str(pin.get("dry_run") or "unsupported") != "supported"
        ):
            summary = "component has no paper mode; nothing executed (launch live after acknowledging no_dry_run)"
            recorder.record_run(
                status="skipped",
                decision={"action": "path_component", "reason": summary},
                mode=mode,
            )
            payload = {
                "ok": True,
                "status": "skipped",
                "skipped": True,
                "summary": summary,
            }
        else:
            timeout = int(job.script_loop.timeout_seconds or DEFAULT_TIMEOUT_S)
            outcome = _exec_component(
                root,
                job_data,
                pin,
                path_dir,
                mode=mode,
                dry_run=mode == "paper",
                timeout=timeout,
            )
            for event in outcome.get("events") or []:
                _record_event(recorder, event, mode=mode)
            status = "ok" if outcome.get("ok") else "failed"
            summary = (
                f"{pin.get('slug')} {pin.get('component')} exit {outcome.get('exit_code')}, "
                f"{len(outcome.get('events') or [])} event(s)"
            )
            recorder.record_run(
                status=status,
                decision={"action": "path_component", "reason": summary},
                metrics={
                    "exit_code": outcome.get("exit_code"),
                    "elapsed_s": outcome.get("elapsed_s"),
                    "events": len(outcome.get("events") or []),
                },
                mode=mode,
                error=outcome.get("error"),
            )
            payload = {
                "ok": bool(outcome.get("ok")),
                "status": status,
                "summary": summary,
                "error": outcome.get("error"),
                "exit_code": outcome.get("exit_code"),
                "timed_out": outcome.get("timed_out"),
                "events": outcome.get("events") or [],
                "stdout_tail": outcome.get("stdout_tail"),
                "stderr_tail": outcome.get("stderr_tail"),
            }
        if divergence is not None:
            payload.setdefault("guard_events", []).append(divergence)
    except PathRefusal as exc:
        payload = {"ok": False, "refused": True, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        payload = {
            "ok": False,
            "error": str(exc),
            "traceback": traceback.format_exc()[-2000:],
        }
    if store is not None and job is not None:
        events: list[str] = []
        if payload.get("ok") is not True:
            events.append("script_failure")
        kinds = {str(e.get("kind")) for e in payload.get("guard_events") or []}
        if kinds & {"risk_halt", "manual_halt"}:
            events.append("risk_halt")
        if "mode_divergence" in kinds:
            events.append("reconcile_mismatch")
        if events:
            fire_triggers(store, job, events, source="scheduled_tick")
    summary = payload.get("summary") or payload.get("error") or "path tick"
    print(json.dumps(payload, default=str))
    print(
        JOB_RESULT_MARKER
        + json.dumps(
            {
                "summary": str(summary)[:1000],
                "severity": "info" if payload.get("ok") else "error",
            }
        )
    )
    return payload


def _pin_mismatch(pin: dict[str, Any], path_dir: Path) -> str | None:
    if not path_dir.is_dir():
        return f"installed path missing at {path_dir}"
    on_disk = bundle_sha256(path_dir)
    if on_disk and str(pin.get("bundle_sha256") or "").lower() not in {
        "",
        on_disk.lower(),
    }:
        return (
            f"bundle.zip changed: pinned {pin.get('bundle_sha256')}, on disk {on_disk}"
        )
    if pin.get("tree_sha256") and tree_sha256(path_dir) != str(pin["tree_sha256"]):
        return "installed files changed since the pin was taken"
    return None


def _note_upgrade(store: JobStore, job: WayfinderJob, pin: dict[str, Any]) -> None:
    """The lock moved to a newer version: say so once per version, keep running the pin."""
    try:
        path_dir = Path(str(pin.get("install_dir") or ""))
        entry, _base, _lock = resolve_install(
            store, install_dir=path_dir.parent.parent, slug=str(pin.get("slug"))
        )
    except Exception:  # noqa: BLE001
        return
    available = str((entry or {}).get("version") or "")
    if not available or available == str(pin.get("version")):
        return
    seen = store.read_json(job.id, UPGRADE_STATE_PATH, default={}) or {}
    if seen.get("available_version") == available:
        return
    store.write_json(
        job.id,
        UPGRADE_STATE_PATH,
        {
            "available_version": available,
            "pinned_version": pin.get("version"),
            "seen_at": utc_now_iso(),
            "hint": f"create a new job from {pin.get('slug')} {available} to move; the pin never moves on its own",
        },
    )
    store.append_journal(
        job.id,
        {
            "type": "path_upgrade_available",
            "pinned": pin.get("version"),
            "available": available,
        },
    )


def _exec_component(
    root: Path,
    job_data: dict[str, Any],
    pin: dict[str, Any],
    path_dir: Path,
    *,
    mode: str,
    dry_run: bool,
    timeout: int,
) -> dict[str, Any]:
    target = path_dir / str(pin.get("component_path") or "")
    state_dir = root / PATH_STATE_DIR
    state_dir.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "WAYFINDER_JOB_MODE": mode,
        "WAYFINDER_PATH_DRY_RUN": "1" if dry_run else "0",
        "WAYFINDER_PATH_PARAMS": json.dumps(dict(pin.get("params") or {})),
        "WAYFINDER_PATH_STATE_DIR": str(state_dir),
        "WAYFINDER_JOB_DIR": str(root),
        "WAYFINDER_HIGH_LEVEL_JOB_ID": str(job_data.get("id") or root.name),
        "PYTHONPATH": os.pathsep.join(
            [str(path_dir), os.environ.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep),
    }
    started = time.monotonic()
    outcome: dict[str, Any] = {"ok": False, "events": [], "timeout_s": timeout}
    try:
        completed = subprocess.run(
            [sys.executable, str(target)],
            cwd=str(path_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        outcome.update(
            {
                "timed_out": True,
                "error": f"component exceeded {timeout}s",
                "elapsed_s": round(time.monotonic() - started, 3),
            }
        )
        return outcome
    stdout = completed.stdout or ""
    events = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(PATH_EVENT_MARKER):
            try:
                event = json.loads(stripped[len(PATH_EVENT_MARKER) :])
            except ValueError:
                continue
            if isinstance(event, dict):
                events.append(event)
    outcome.update(
        {
            "ok": completed.returncode == 0,
            "exit_code": completed.returncode,
            "events": events,
            "stdout_tail": stdout[-2000:],
            "stderr_tail": (completed.stderr or "")[-2000:],
            "elapsed_s": round(time.monotonic() - started, 3),
            "error": None
            if completed.returncode == 0
            else f"exit {completed.returncode}: {(completed.stderr or '')[-500:]}",
        }
    )
    return outcome


def _record_event(
    recorder: ForwardRecorder, event: dict[str, Any], *, mode: str
) -> None:
    kind = str(event.get("type") or "")
    payload = dict(event.get("payload") or {})
    payload.setdefault("mode", mode)
    payload.setdefault("source", "path")
    if kind == "order":
        recorder.record_order(payload)
    elif kind == "fill":
        recorder.record_fill(payload)
    elif kind == "trade_close":
        recorder.record_trade_close(payload)
    elif kind == "trade_open":
        recorder.record_trade_open(payload)
    elif kind == "funding":
        recorder.record_funding(payload)
    elif kind in {"tick", "state_snapshot"}:
        recorder.record_tick(payload)


__all__ = [
    "PIN_PATH",
    "UPGRADE_STATE_PATH",
    "create_from_path",
    "run_path_tick",
    "tree_sha256",
    "validate_path_job",
]
