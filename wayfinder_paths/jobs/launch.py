"""Launch checklist and launch: prove that what was validated is what will
run, name every missing risk parameter, then pin the revision and start the
loops.

The checklist is the one place identity is compared: the validation (and,
for jobs_v1, backtest and preflight) reports carry the workspace revision
they were produced at; a paper launch refuses on any mismatch. Live adds the
kind's live rule: ``evaluate_live_gate`` for jobs_v1, and for freestyle/path
jobs a wallet, a risk-limits file, enough paper runs and every warn-level
risk flag acknowledged.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from wayfinder_paths.core.strategies.risk_limits import RiskLimits
from wayfinder_paths.jobs.application import pause_job_loops, resume_job_loops
from wayfinder_paths.jobs.compiler import JobCompiler
from wayfinder_paths.jobs.gating import compute_workspace_revision, evaluate_live_gate
from wayfinder_paths.jobs.models import (
    LIFECYCLE_CONTRACTS,
    default_wake_seconds,
    infer_job_kind,
    normalize_agent_mode,
    utc_now_iso,
)
from wayfinder_paths.jobs.notify_policy import (
    normalize_notifications,
    notifications_for,
)
from wayfinder_paths.jobs.risk_flags import (
    acknowledge_risk_flags,
    risk_flags,
    unacknowledged_risk_flags,
)
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.sync import apply_script_mode, sync_all_jobs
from wayfinder_paths.jobs.triggers import ALWAYS_WAKE_EVENTS
from wayfinder_paths.runner.schedule import normalize_schedule

LAUNCH_STATE_PATH = "state/launch.json"
LAUNCH_LOG_PATH = "versions/launches.jsonl"
DEFAULT_MIN_PAPER_RUNS = 20


def evaluate_launch_checklist(
    job_id: str,
    *,
    store: JobStore | None = None,
    target: str = "paper",
) -> dict[str, Any]:
    if target not in {"paper", "live"}:
        raise ValueError("target must be 'paper' or 'live'")
    store = store or JobStore()
    job = store.load(job_id)
    root = store.job_dir(job_id)
    contract = str(job.execution_contract or "legacy")
    revision = compute_workspace_revision(root)
    items: list[dict[str, Any]] = []
    identity: dict[str, Any] = {"workspace": revision}

    def item(item_id: str, status: str, detail: str, **extra: Any) -> None:
        items.append({"id": item_id, "status": status, "detail": detail, **extra})

    if contract not in LIFECYCLE_CONTRACTS:
        item(
            "contract",
            "fail",
            f"{contract} jobs are outside the launch flow; create it as jobs_v1 or freestyle_v1",
        )

    validation = store.read_json(job_id, "reports/validation/latest.json", default=None)
    if not validation:
        item("validation_present", "fail", "no validation report: run validate first")
    else:
        identity["validation"] = validation.get("revision")
        item(
            "validation_present",
            "pass",
            f"validation report at {validation.get('revision')}",
        )
        if validation.get("revision") != revision:
            item(
                "validation_at_revision",
                "fail",
                f"validated {validation.get('revision')}, deployed {revision}: re-run validate",
            )
        else:
            item(
                "validation_at_revision",
                "pass",
                "validated revision matches the workspace",
            )
        failed = [
            c.get("name")
            for c in validation.get("checks") or []
            if not c.get("passed") and c.get("blocking") is not False
        ]
        if validation.get("status") != "passed":
            item("validation_passed", "fail", f"validation failed: {failed}")
        else:
            item("validation_passed", "pass", "all blocking validation checks passed")

    if contract == "jobs_v1":
        _jobs_v1_items(store, job_id, revision, target, identity, item)
    elif contract in {"freestyle_v1", "path_v1"}:
        _mechanical_items(validation or {}, contract, identity, item)

    mode = str(job.script_loop.mode or "paper")
    if target == "paper" and mode == "live":
        item(
            "mode_is_paper",
            "fail",
            "the job is already live; leave live before relaunching in paper",
        )
    else:
        item(
            "mode_is_paper" if target == "paper" else "current_mode",
            "pass",
            f"current mode {mode}",
        )

    flags = risk_flags(job, root)
    split = unacknowledged_risk_flags(store, job_id, flags)
    unacknowledged = {flag["code"] for flag in split["unacknowledged"]}
    for flag in flags:
        if flag["severity"] == "block":
            status = "fail"
        elif (
            flag["severity"] == "warn"
            and target == "live"
            and flag["code"] in unacknowledged
        ):
            status = "ack_required"
        elif flag["severity"] == "warn":
            status = "warn"
        else:
            status = "info"
        item(
            f"risk:{flag['code']}",
            status,
            flag["message"],
            fix=flag["fix"],
            scope=flag["scope"],
        )

    if target == "live":
        if contract == "jobs_v1":
            gate = evaluate_live_gate(job_id, store=store)
            identity.update(
                {
                    "backtest": (gate.get("backtest") or {}).get("revision"),
                    "preflight": (gate.get("preflight") or {}).get("revision"),
                }
            )
            item(
                "live_gate",
                "pass" if gate["live_ready"] else "fail",
                "live gate ready" if gate["live_ready"] else "; ".join(gate["reasons"]),
            )
        else:
            wallet = job.execution_params.get("wallet_label")
            item(
                "wallet_label",
                "pass" if wallet else "fail",
                f"wallet {wallet}"
                if wallet
                else "execution_params.wallet_label is not set",
            )
            limits = RiskLimits.load_optional(root / "workspace")
            has_limits = limits is not None and (
                limits.max_daily_loss_usd is not None or limits.max_drawdown is not None
            )
            item(
                "risk_limits_file",
                "pass" if has_limits else "fail",
                "risk limits declare a daily loss or drawdown cap"
                if has_limits
                else "live needs a daily loss or drawdown cap: set_watchdog(kill_switches={…}) writes workspace/risk_limits.json and relaunches",
            )
            runs = _paper_runs(store, job_id)
            minimum = int(
                (job.execution_params.get("freestyle") or {}).get("min_paper_runs")
                or DEFAULT_MIN_PAPER_RUNS
            )
            item(
                "min_paper_runs",
                "pass" if runs >= minimum else "fail",
                f"{runs} paper runs recorded (minimum {minimum})",
                runs=runs,
                minimum=minimum,
            )

    failures = [i for i in items if i["status"] == "fail"]
    ack_required = [i for i in items if i["status"] == "ack_required"]
    reasons = [i["detail"] for i in failures] + [
        f"acknowledge risk flag {i['id'][5:]}" for i in ack_required
    ]
    ready = not failures and not ack_required
    return {
        "job_id": job_id,
        "kind": contract,
        "target": target,
        "ok": ready,
        "revision": revision,
        "identity": identity,
        "items": items,
        "risk_flags": flags,
        "unacknowledged": sorted(unacknowledged),
        "ready_paper": ready if target == "paper" else None,
        "ready_live": ready if target == "live" else None,
        "reasons": reasons,
        "checked_at": utc_now_iso(),
    }


def launch_job(
    job_id: str,
    *,
    store: JobStore | None = None,
    script_mode: str = "paper",
    confirm_live: bool = False,
    acknowledge: list[str] | tuple[str, ...] = (),
    by: str = "owner",
) -> dict[str, Any]:
    """Pin the current revision, compile it into the runner and resume the
    loops. Returns ``launched: False`` with the checklist instead of raising
    so the chat surface can read the blockers back."""
    if script_mode not in {"paper", "live"}:
        raise ValueError("script_mode must be 'paper' or 'live'")
    store = store or JobStore()
    if acknowledge:
        acknowledge_risk_flags(store, job_id, list(acknowledge), by=by)
    checklist = evaluate_launch_checklist(job_id, store=store, target=script_mode)
    if not checklist["ok"]:
        return {
            "launched": False,
            "job_id": job_id,
            "mode": script_mode,
            "checklist": checklist,
        }
    if script_mode == "live" and not confirm_live:
        return {
            "launched": False,
            "job_id": job_id,
            "mode": script_mode,
            "checklist": checklist,
            "reason": "a live launch needs confirm_live=True after reading the checklist",
        }
    job = store.load(job_id)
    revision = checklist["revision"]
    job.versioning["active_revision"] = revision
    store.save(job)
    stamp = utc_now_iso()
    launch_state = {
        "revision": revision,
        "mode": script_mode,
        "launched_at": stamp,
        "by": by,
        "checklist": {"items": checklist["items"], "identity": checklist["identity"]},
        "flags_shown": [flag["code"] for flag in checklist["risk_flags"]],
    }
    store.write_json(job_id, LAUNCH_STATE_PATH, launch_state)
    log_path = store.job_dir(job_id) / LAUNCH_LOG_PATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "revision": revision,
                    "mode": script_mode,
                    "launched_at": stamp,
                    "by": by,
                }
            )
            + "\n"
        )
    compile_result = JobCompiler(store=store).compile(job)
    loops = resume_job_loops(store, job_id)
    store.append_journal(
        job_id,
        {
            "type": "launched",
            "revision": revision,
            "mode": script_mode,
            "by": by,
            "risk_flags_shown": launch_state["flags_shown"],
        },
    )
    mode_result: dict[str, Any] | None = None
    if script_mode == "live":
        mode_result = apply_script_mode(job_id, "live", store=store, set_by=by)
    else:
        sync_all_jobs(store=store)
    return {
        "launched": True,
        "job_id": job_id,
        "mode": script_mode,
        "revision": revision,
        "checklist": checklist,
        "compile": compile_result,
        "loops": loops,
        "script_mode": mode_result,
    }


def hold_job(
    job_id: str, *, store: JobStore | None = None, by: str = "owner"
) -> dict[str, Any]:
    """Create-time counterpart of launch: keep the loops paused until launch."""
    store = store or JobStore()
    loops = pause_job_loops(store, job_id)
    store.append_journal(job_id, {"type": "created_unlaunched", "by": by})
    return {"job_id": job_id, "paused": True, "loops": loops}


def _jobs_v1_items(
    store: JobStore,
    job_id: str,
    revision: str,
    target: str,
    identity: dict[str, Any],
    item: Any,
) -> None:
    backtest = store.read_json(job_id, "results/backtest/latest.json", default=None)
    if not backtest:
        item(
            "backtest_present",
            "warn",
            "no backtest artifact: the readout has nothing to say yet",
        )
    else:
        identity["backtest"] = backtest.get("revision")
        at_revision = backtest.get("revision") == revision
        item(
            "backtest_at_revision",
            "pass" if at_revision else "warn",
            "backtest matches the workspace revision"
            if at_revision
            else f"backtest is for {backtest.get('revision')}, workspace is {revision}",
        )
    preflight = store.read_json(job_id, "reports/preflight/latest.json", default=None)
    severity = "fail" if target == "live" else "warn"
    if not preflight:
        item(
            "preflight_present",
            severity,
            "no preflight report (run `wayfinder job preflight`)",
        )
    else:
        identity["preflight"] = preflight.get("revision")
        ok = (
            preflight.get("status") == "passed"
            and preflight.get("revision") == revision
        )
        item(
            "preflight_at_revision",
            "pass" if ok else severity,
            "preflight passed at the workspace revision"
            if ok
            else f"preflight {preflight.get('status')} at {preflight.get('revision')}, workspace is {revision}",
        )


def _mechanical_items(
    validation: dict[str, Any], contract: str, identity: dict[str, Any], item: Any
) -> None:
    checks = {c.get("name"): c for c in validation.get("checks") or []}
    dry = checks.get("dry_run_ok") or checks.get("dry_run_exec")
    if dry is None:
        item("mechanical_dry_run", "fail", "no dry run recorded: run validate")
    elif dry.get("passed"):
        item(
            "mechanical_dry_run",
            "pass",
            f"dry run passed ({dry.get('ticks') or 1} tick(s))",
        )
    else:
        item("mechanical_dry_run", "fail", f"dry run failed: {dry.get('error')}")
    if contract == "path_v1":
        pin = checks.get("bundle_sha256_verified")
        section = validation.get("path") or {}
        identity["path_pin"] = section.get("pin")
        if pin is None or not pin.get("passed"):
            item(
                "path_pin",
                "fail",
                "the installed bundle does not match the pinned path version",
            )
        else:
            item("path_pin", "pass", "installed bundle matches the pin")


def _paper_runs(store: JobStore, job_id: str) -> int:
    summary = store.read_json(job_id, "results/forward/summary.json", default={}) or {}
    runs = summary.get("runs") or {}
    return int(runs.get("count") or 0)


WATCH_LEVELS: tuple[str, ...] = ("off", "monitor", "intervene", "auto")
KNOWN_TRIGGER_EVENTS: frozenset[str] = (
    frozenset(
        {
            "script_failure",
            "drift_warning",
            "health_red",
            "proposal_created",
            "reconcile_mismatch",
            "risk_halt",
            "regime_shift",
        }
    )
    | ALWAYS_WAKE_EVENTS
)
KILL_SWITCH_KEYS: frozenset[str] = frozenset(
    {
        "max_drawdown",
        "max_daily_loss_usd",
        "pause_after_consecutive_losses",
        "max_gross_exposure_usd",
        "max_position_per_symbol_usd",
    }
)
RISK_LIMITS_PATH = "workspace/risk_limits.json"


def watchdog_view(job: Any, root: Path) -> dict[str, Any]:
    """The long watchdog as one object: watch level, cadence, triggers,
    notification policy and kill switches."""
    limits = store_read_json(root / RISK_LIMITS_PATH) or {}
    loop = job.agent_loop
    return {
        "watch_level": str(loop.mode),
        "enabled": bool(loop.enabled),
        "wake_interval_seconds": loop.wake_interval_seconds,
        "cron_expr": loop.cron_expr,
        "timezone": loop.timezone,
        "triggers": list(loop.triggers),
        "always_wake": sorted(ALWAYS_WAKE_EVENTS),
        "trigger_debounce_seconds": loop.trigger_debounce_seconds,
        "notifications": notifications_for(job),
        "kill_switches": {k: v for k, v in limits.items() if k in KILL_SWITCH_KEYS},
    }


def set_watchdog(
    job_id: str,
    *,
    store: JobStore | None = None,
    watch_level: str | None = None,
    wake_interval_seconds: int | None = None,
    cron_expr: str | None = None,
    timezone: str | None = None,
    triggers: list[str] | None = None,
    trigger_debounce_seconds: int | None = None,
    notifications: dict[str, Any] | None = None,
    kill_switches: dict[str, Any] | None = None,
    by: str = "owner",
) -> dict[str, Any]:
    """Customize the long watchdog. Watch level, cadence and triggers are
    operator dials (outside the revision hash); kill switches live in
    workspace/risk_limits.json, so changing them moves the revision: jobs_v1
    kicks the gate restamp, a launched freestyle/path job is re-validated and
    re-launched in its current mode, and a failed re-validation restores the
    previous limits so the running loop is never orphaned."""
    store = store or JobStore()
    job = store.load(job_id)
    root = store.job_dir(job_id)
    contract = str(job.execution_contract or "legacy")
    changes: dict[str, Any] = {}
    warnings: list[str] = []
    if watch_level is not None:
        if watch_level not in WATCH_LEVELS:
            raise ValueError(
                f"watch_level must be one of {WATCH_LEVELS}, got {watch_level!r}"
            )
        mode = normalize_agent_mode(watch_level)
        changes["watch_level"] = {"from": job.agent_loop.mode, "to": mode}
        job.agent_loop.mode = mode
        job.agent_loop.enabled = mode != "off"
        job.job_kind = infer_job_kind(job.script_loop.enabled, mode)
    if wake_interval_seconds is not None:
        if int(wake_interval_seconds) < 60:
            raise ValueError("wake_interval_seconds must be at least 60")
        changes["wake_interval_seconds"] = {
            "from": job.agent_loop.wake_interval_seconds,
            "to": int(wake_interval_seconds),
        }
        job.agent_loop.wake_interval_seconds = int(wake_interval_seconds)
        job.agent_loop.cron_expr = None
    if cron_expr is not None:
        normalize_schedule(interval_seconds=None, cron_expr=cron_expr)
        changes["cron_expr"] = {"from": job.agent_loop.cron_expr, "to": cron_expr}
        job.agent_loop.cron_expr = cron_expr
        job.agent_loop.wake_interval_seconds = None
    if timezone:
        job.agent_loop.timezone = str(timezone)
    if (
        job.agent_loop.enabled
        and not job.agent_loop.wake_interval_seconds
        and not job.agent_loop.cron_expr
    ):
        job.agent_loop.wake_interval_seconds = default_wake_seconds(job.agent_loop.mode)
    if triggers is not None:
        unknown = sorted(set(triggers) - KNOWN_TRIGGER_EVENTS)
        if unknown:
            raise ValueError(
                f"unknown trigger events {unknown}; known: {sorted(KNOWN_TRIGGER_EVENTS)}"
            )
        changes["triggers"] = {
            "from": list(job.agent_loop.triggers),
            "to": sorted(set(triggers)),
        }
        job.agent_loop.triggers = sorted(set(triggers))
    if trigger_debounce_seconds is not None:
        job.agent_loop.trigger_debounce_seconds = int(trigger_debounce_seconds)
        changes["trigger_debounce_seconds"] = int(trigger_debounce_seconds)
    if notifications is not None:
        normalized = normalize_notifications(notifications)
        changes["notifications"] = normalized
        job.reporting = {**(job.reporting or {}), "notify": normalized}
    previous_limits_text: str | None = None
    workspace_changed = False
    if kill_switches is not None:
        unknown_keys = sorted(set(kill_switches) - KILL_SWITCH_KEYS)
        if unknown_keys:
            raise ValueError(
                f"unknown kill switches {unknown_keys}; known: {sorted(KILL_SWITCH_KEYS)}"
            )
        limits_path = root / RISK_LIMITS_PATH
        previous_limits_text = (
            limits_path.read_text(encoding="utf-8") if limits_path.exists() else None
        )
        merged = dict(store_read_json(limits_path) or {})
        for key, value in kill_switches.items():
            if value is None:
                merged.pop(key, None)
                continue
            number = float(value)
            merged[key] = (
                -abs(number)
                if key == "max_drawdown"
                else (
                    int(number) if key == "pause_after_consecutive_losses" else number
                )
            )
        changes["kill_switches"] = {
            k: v for k, v in merged.items() if k in KILL_SWITCH_KEYS
        }
        store.write_json(job_id, RISK_LIMITS_PATH, merged)
        workspace_changed = True
    job.touch()
    store.save(job)
    store.append_journal(job_id, {"type": "watchdog_set", "changes": changes, "by": by})
    compile_result = JobCompiler(store=store).compile(job)
    if contract == "jobs_v1" and job.agent_loop.mode in {"off", "monitor"}:
        warnings.append(
            "watch level off/monitor makes this job evolution-ineligible (evolution needs intervene or auto)"
        )
    eligibility: dict[str, Any]
    if contract == "jobs_v1":
        from wayfinder_paths.jobs.improver.spec import ImproverSpec

        eligibility = ImproverSpec.load(root).evolution_eligibility(root, job_id)
    else:
        eligibility = {
            "eligible": False,
            "reasons": ["freestyle and path jobs do not evolve"],
        }
    restamp: dict[str, Any] | None = None
    relaunch: dict[str, Any] | None = None
    if workspace_changed:
        if contract == "jobs_v1":
            try:
                from wayfinder_paths.jobs.background import spawn_detached_op

                restamp = spawn_detached_op(
                    store, job_id, "restamp", {"job_id": job_id}
                )
                store.append_journal(
                    job_id, {"type": "gate_restamp_kicked", "trigger": "set_watchdog"}
                )
            except Exception as exc:  # noqa: BLE001 — the dial must not fail on this
                restamp = {"error": str(exc)[:200]}
        else:
            launched = store.read_json(job_id, LAUNCH_STATE_PATH, default=None)
            if launched:
                from wayfinder_paths.jobs.contracts import validate_job_for_kind

                report = validate_job_for_kind(job_id, store=store)
                if report.get("status") != "passed":
                    limits_path = root / RISK_LIMITS_PATH
                    if previous_limits_text is None:
                        limits_path.unlink(missing_ok=True)
                    else:
                        limits_path.write_text(previous_limits_text, encoding="utf-8")
                    validate_job_for_kind(job_id, store=store)
                    store.append_journal(
                        job_id, {"type": "watchdog_kill_switches_reverted", "by": by}
                    )
                    raise ValueError(
                        "kill switches rejected: validation failed after the change, the previous limits stay"
                    )
                launched_mode = str(launched.get("mode") or "paper")
                relaunch = launch_job(
                    job_id,
                    store=store,
                    script_mode=launched_mode,
                    confirm_live=launched_mode == "live",
                    by=by,
                )
            else:
                # Not launched yet: validate now so the launch that follows
                # (or rides along with these settings) finds the validation
                # stamp at the new revision instead of a stale one.
                from wayfinder_paths.jobs.contracts import validate_job_for_kind

                report = validate_job_for_kind(job_id, store=store)
                if report.get("status") != "passed":
                    warnings.append(
                        "kill switches changed the workspace and validation now fails: "
                        "fix the job before launching"
                    )
    sync_all_jobs(store=store)
    return {
        "job_id": job_id,
        "watchdog": watchdog_view(store.load(job_id), root),
        "changes": changes,
        "compile": compile_result,
        "evolution_eligibility": eligibility,
        "warnings": warnings,
        "restamp": restamp,
        "relaunch": relaunch,
    }


def repin_launch(
    store: JobStore, job_id: str, *, revision: str, by: str
) -> dict[str, Any] | None:
    """An applied proposal moved the workspace: the launch pin follows it so
    identity (validated == deployed == launched) holds without a re-launch.
    No-op on a job that was never launched."""
    launched = store.read_json(job_id, LAUNCH_STATE_PATH, default=None)
    if not launched or not revision:
        return None
    stamp = utc_now_iso()
    state = {
        **launched,
        "revision": revision,
        "relaunched_at": stamp,
        "relaunched_by": by,
    }
    store.write_json(job_id, LAUNCH_STATE_PATH, state)
    log_path = store.job_dir(job_id) / LAUNCH_LOG_PATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "revision": revision,
                    "mode": state.get("mode"),
                    "launched_at": stamp,
                    "by": by,
                    "repin": True,
                }
            )
            + "\n"
        )
    store.append_journal(
        job_id,
        {
            "type": "launch_repinned",
            "revision": revision,
            "from_revision": launched.get("revision"),
            "by": by,
        },
    )
    return state


def store_read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


__all__ = [
    "LAUNCH_STATE_PATH",
    "evaluate_launch_checklist",
    "hold_job",
    "launch_job",
    "repin_launch",
    "set_watchdog",
    "watchdog_view",
]
