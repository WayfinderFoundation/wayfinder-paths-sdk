"""Heartbeat and issues: the runtime truth of a job as one block, and a flat
list of what is wrong, both composed from artifacts the box already keeps.

``heartbeat`` answers "is it running, when did it last run, when is it due,
what revision is it on, is it halted" without a tool call per fact.
``issues`` turns those facts (plus declared features, risk flags and the
launch checklist) into owner-facing rows with a closed vocabulary of codes,
sorted block > warn > info. Every detector is raise-free — a sync must never
break because one artifact is missing or malformed — and nothing here reads
``ticks.jsonl`` (it grows without bound; the last tick lives in a one-row
file the freestyle runtime writes).
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from wayfinder_paths.jobs.execution.primitives import bar_interval_seconds
from wayfinder_paths.jobs.halt import RISK_LATCH_SOURCES
from wayfinder_paths.jobs.models import LIFECYCLE_CONTRACTS
from wayfinder_paths.jobs.store import JobStore

FREESTYLE_LAST_TICK_PATH = "state/freestyle_last_tick.json"
FORWARD_SUMMARY_PATH = "results/forward/summary.json"
JOURNAL_TAIL_ROWS = 400
APPLY_STALL_SECONDS = 15 * 60
# A reverted apply stays an owner-facing issue for a week (the window the
# decided_autonomously feed also uses); a later successful apply of the same
# proposal clears the marker earlier.
APPLY_REVERT_WINDOW_SECONDS = 7 * 24 * 3600
FEED_STALE_MULTIPLIER = 2
SCRIPT_ERROR_FAILURES = 3
READ_FAILURE_MARKERS = ("mark failed for", "quote failed", "read failed")

ISSUE_CODES: tuple[str, ...] = (
    "runner_unreachable",
    "script_loop_error",
    "script_loop_paused",
    "tick_overdue",
    "tick_failed",
    "agent_wake_overdue",
    "agent_wake_failed",
    "revision_drift",
    "mode_mismatch",
    "halted",
    "feed_stale",
    "read_failed",
    "dataset_fetch_failed",
    "risk_flags_unacknowledged",
    "launch_checklist_failing",
    "unpapered_actions",
    "apply_stalled",
    "apply_reverted",
    "not_launched",
)
SEVERITY_RANK = {"block": 0, "warn": 1, "info": 2}


def sdk_version() -> str | None:
    try:
        return importlib.metadata.version("wayfinder-paths")
    except importlib.metadata.PackageNotFoundError:
        return None


def _unix_to_iso(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(float(value), tz=UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _ago(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{int(seconds)} s"
    if seconds < 5400:
        return f"{int(seconds // 60)} min"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} d"


def _loop_state(enabled: bool, runner_job_name: str, state: Any) -> dict[str, Any]:
    state = state if isinstance(state, dict) else {}
    return {
        "enabled": bool(enabled),
        "runner_job_name": runner_job_name or None,
        "runner_status": str(state["status"]) if state.get("status") else None,
        "last_run_at": _unix_to_iso(state.get("last_run_at")),
        "last_ok_at": _unix_to_iso(state.get("last_ok_at")),
        "next_run_at": _unix_to_iso(state.get("next_run_at")),
        "consecutive_failures": int(state.get("consecutive_failures") or 0),
        "last_error": str(state["last_error"]) if state.get("last_error") else None,
    }


def _last_tick(
    store: JobStore, job_id: str, job: Any, runs: dict[str, Any], script: dict[str, Any]
) -> dict[str, Any] | None:
    contract = str(job.execution_contract or "legacy")
    if contract in {"freestyle_v1", "path_v1"}:
        tick = store.read_json(job_id, FREESTYLE_LAST_TICK_PATH, default=None)
        if isinstance(tick, dict) and tick.get("ts"):
            return {
                "ts": tick.get("ts"),
                "status": tick.get("status"),
                "summary": tick.get("summary"),
                "error": tick.get("error"),
                "mode": tick.get("mode"),
                "revision": tick.get("revision"),
            }
    if not runs.get("last_run_at"):
        return None
    failures = int(script.get("consecutive_failures") or 0)
    decision = runs.get("last_decision")
    reason = runs.get("last_reason")
    summary = None
    if decision or reason:
        summary = (
            f"{decision}: {reason}" if decision and reason else str(decision or reason)
        )
    return {
        "ts": runs.get("last_run_at"),
        "status": "failed" if failures else "ok",
        "summary": summary,
        "error": script.get("last_error") if failures else None,
        "mode": None,
        "revision": None,
    }


def build_heartbeat(
    store: JobStore,
    job_id: str,
    job: Any,
    *,
    runner_states: dict[str, Any],
    reports: dict[str, Any],
    launch: dict[str, Any] | None,
    halt: dict[str, Any] | None,
    workspace_revision: str | None,
) -> dict[str, Any]:
    script = job.script_loop
    agent = job.agent_loop
    states = runner_states or {}
    script_state = states.get(script.runner_job_name) if script.enabled else None
    agent_state = states.get(agent.runner_job_name) if agent.enabled else None
    loops_enabled = bool(script.enabled or agent.enabled)
    script_block = {
        **_loop_state(script.enabled, script.runner_job_name, script_state),
        "interval_seconds": script.interval_seconds,
        "cron_expr": script.cron_expr,
    }
    agent_block = {
        **_loop_state(agent.enabled, agent.runner_job_name, agent_state),
        "wake_interval_seconds": agent.wake_interval_seconds,
        "cron_expr": agent.cron_expr,
        "last_report_at": {
            mode: ((reports or {}).get(mode) or {}).get("created_at")
            for mode in ("monitor", "intervene", "auto", "apply")
        },
    }
    summary = store.read_json(job_id, FORWARD_SUMMARY_PATH, default={}) or {}
    runs = summary.get("runs") or {}
    ticks = summary.get("ticks") or {}
    env = ((script_state or {}).get("payload") or {}).get("env") or {}
    active_revision = (
        str(env.get("WAYFINDER_JOB_REVISION") or "")
        or str((job.versioning or {}).get("active_revision") or "")
        or None
    )
    launch = launch if isinstance(launch, dict) else None
    launch_revision = str(launch.get("revision") or "") or None if launch else None
    identity_ok: bool | None = None
    if launch and launch_revision and workspace_revision:
        identity_ok = launch_revision == workspace_revision and (
            active_revision is None or active_revision == launch_revision
        )
    halt = halt if isinstance(halt, dict) else None
    return {
        "sdk_version": sdk_version(),
        "runner_reachable": bool(states) if loops_enabled else None,
        "loops": {"script": script_block, "agent": agent_block},
        "last_tick": _last_tick(store, job_id, job, runs, script_block),
        "ticks": {
            "count": int(ticks.get("count") or runs.get("count") or 0),
            "error_count": int(runs.get("error_count") or 0),
            "last_tick_at": ticks.get("last_tick_at") or runs.get("last_run_at"),
        },
        "launch": {
            "launched": bool(launch),
            "revision": launch_revision,
            "mode": launch.get("mode") if launch else None,
            "launched_at": launch.get("launched_at") if launch else None,
            "relaunched_at": launch.get("relaunched_at") if launch else None,
            "by": launch.get("by") if launch else None,
            "active_revision": active_revision,
            "workspace_revision": workspace_revision,
            "identity_ok": identity_ok,
        },
        "halt": {
            "active": bool(halt),
            "reason": halt.get("reason") if halt else None,
            "source": halt.get("source") if halt else None,
            "ts": halt.get("ts") if halt else None,
            "flatten": bool(halt.get("flatten")) if halt else False,
        },
    }


def build_issues(
    store: JobStore,
    job_id: str,
    job: Any,
    *,
    heartbeat: dict[str, Any] | None,
    scorecard: dict[str, Any] | None,
    features: list[dict[str, Any]] | None,
    risk_flags: list[dict[str, Any]] | None,
    launch_checklist: dict[str, Any] | None,
    proposals: list[dict[str, Any]] | None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    now = now or datetime.now(UTC)
    hb = heartbeat or {}
    loops = hb.get("loops") or {}
    script = loops.get("script") or {}
    agent = loops.get("agent") or {}
    launch = hb.get("launch") or {}
    halt = hb.get("halt") or {}
    last_tick = hb.get("last_tick") or {}
    ticks = hb.get("ticks") or {}
    reachable = hb.get("runner_reachable")
    scorecard = scorecard or {}
    contract = str(job.execution_contract or "legacy")
    issues: list[dict[str, Any]] = []

    def add(
        code: str,
        severity: str,
        message: str,
        *,
        since: Any = None,
        fix: str | None = None,
        scope: str | None = None,
        ref: str | None = None,
    ) -> None:
        issues.append(
            {
                "code": code,
                "severity": severity,
                "message": message,
                "since": since,
                "fix": fix,
                "scope": scope,
                "ref": ref,
            }
        )

    def runner_unreachable() -> None:
        if reachable is False:
            add(
                "runner_unreachable",
                "warn",
                "the runner daemon is not reporting; the loops may not be running",
                scope="runtime",
                fix="check the runner (wayfinder runner status) and restart it",
            )

    def script_loop() -> None:
        if not script.get("enabled") or not reachable:
            return
        failures = int(script.get("consecutive_failures") or 0)
        status = script.get("runner_status")
        error = script.get("last_error")
        if status == "ERROR" or failures >= SCRIPT_ERROR_FAILURES:
            add(
                "script_loop_error",
                "block",
                f"script loop failing: {error or f'{failures} failures in a row'}",
                since=script.get("last_ok_at"),
                scope="script",
                fix="read the last error; fix the job through a proposal, then validate and launch again",
            )
        elif status == "PAUSED" and launch.get("launched"):
            in_flight = any(
                ((p.get("application") or {}).get("status") in {"queued", "applying"})
                for p in (proposals or [])
            )
            if not in_flight:
                add(
                    "script_loop_paused",
                    "info",
                    "the script loop is paused",
                    scope="script",
                    fix="resume the job to tick again",
                )

    def tick_failed() -> None:
        failures = int(script.get("consecutive_failures") or 0)
        if last_tick.get("status") == "failed" or 0 < failures < SCRIPT_ERROR_FAILURES:
            add(
                "tick_failed",
                "warn",
                f"the last tick failed: {last_tick.get('error') or script.get('last_error') or 'no error text'}",
                since=last_tick.get("ts") or script.get("last_run_at"),
                scope="script",
            )

    def tick_overdue() -> None:
        if not script.get("enabled"):
            return
        interval = int(script.get("interval_seconds") or 0)
        if reachable:
            due = _parse_iso(script.get("next_run_at"))
            if due and interval and (now - due).total_seconds() > interval:
                add(
                    "tick_overdue",
                    "warn",
                    f"the next tick was due {_ago((now - due).total_seconds())} ago",
                    since=script.get("next_run_at"),
                    scope="script",
                )
            return
        from wayfinder_paths.jobs.watchdog import LOOP_GAP_INTERVAL_MULTIPLIER

        last = _parse_iso(ticks.get("last_tick_at"))
        if (
            last
            and interval
            and launch.get("launched")
            and (now - last).total_seconds() > LOOP_GAP_INTERVAL_MULTIPLIER * interval
        ):
            add(
                "tick_overdue",
                "warn",
                f"no tick for {_ago((now - last).total_seconds())} with the runner not reporting",
                since=ticks.get("last_tick_at"),
                scope="script",
            )

    def agent_wake() -> None:
        if not agent.get("enabled"):
            return
        failures = int(agent.get("consecutive_failures") or 0)
        if failures:
            add(
                "agent_wake_failed",
                "warn",
                f"the last agent wake failed: {agent.get('last_error') or f'{failures} failures in a row'}",
                since=agent.get("last_ok_at"),
                scope="agent",
            )
        # A wake the worker could not hand to OpenCode exits 0 for the runner,
        # so it never shows up as a runner failure — the scorecard carries
        # it beside (not over) the last real check.
        wake_error_at = _parse_iso(scorecard.get("last_agent_wake_error_at"))
        check_at = _parse_iso(scorecard.get("last_agent_check_at"))
        if (
            not failures
            and wake_error_at
            and (check_at is None or wake_error_at > check_at)
        ):
            add(
                "agent_wake_failed",
                "warn",
                f"the last agent wake could not start: {scorecard.get('last_agent_wake_error') or 'OpenCode did not answer'}",
                since=scorecard.get("last_agent_wake_error_at"),
                scope="agent",
                fix="check OpenCode on the box; the wake is retried on the next schedule or trigger",
            )
        interval = int(agent.get("wake_interval_seconds") or 0)
        due = _parse_iso(agent.get("next_run_at"))
        if reachable and due and interval and (now - due).total_seconds() > interval:
            add(
                "agent_wake_overdue",
                "warn",
                f"the next agent wake was due {_ago((now - due).total_seconds())} ago",
                since=agent.get("next_run_at"),
                scope="agent",
            )

    def revision_drift() -> None:
        rows = store.read_jsonl(job_id, "journal.jsonl", limit=JOURNAL_TAIL_ROWS)
        drifts = [row for row in rows if row.get("type") == "revision_drift"]
        if not drifts:
            return
        latest = drifts[-1]
        pinned_at = _parse_iso(launch.get("relaunched_at") or launch.get("launched_at"))
        drift_at = _parse_iso(latest.get("ts"))
        if pinned_at and drift_at and drift_at <= pinned_at:
            return
        add(
            "revision_drift",
            "block",
            str(
                latest.get("error")
                or "the workspace changed since the launch; ticks refuse to run"
            ),
            since=latest.get("ts"),
            scope="launch",
            fix="re-run validate and launch",
        )

    def mode_mismatch() -> None:
        if scorecard.get("mode_mismatch"):
            add(
                "mode_mismatch",
                "block",
                "the runner is executing a different mode than the job declares "
                f"(declared {job.script_loop.mode}, running {scorecard.get('mode')})",
                scope="runtime",
                fix="set the mode through the job (set_script_mode or launch), never by hand",
            )
        elif scorecard.get("agent_mode_mismatch"):
            add(
                "mode_mismatch",
                "block",
                "the runner wakes the agent under a different watch level than the job declares",
                scope="runtime",
                fix="set the watch level through set_watchdog so the runner recompiles",
            )

    def halted() -> None:
        if not halt.get("active"):
            return
        source = str(halt.get("source") or "manual")
        latched = source in RISK_LATCH_SOURCES
        add(
            "halted",
            "block" if latched else "warn",
            f"halted: {halt.get('reason') or 'no reason recorded'}",
            since=halt.get("ts"),
            scope="risk",
            fix="clear the halt (resume_from_halt) once the cause is understood",
            ref="owner_attention:halt_awaiting_owner_clear" if latched else None,
        )

    def feed_stale() -> None:
        for entry in features or []:
            name = entry.get("name")
            if not entry.get("available"):
                add(
                    "feed_stale",
                    "warn",
                    f"feature {name} has no rows yet",
                    scope="data",
                    fix="fetch the feature (fetch_token_features / fetch_yield_features / fetch_funding)",
                )
                continue
            cadence = entry.get("cadence")
            age = entry.get("age_seconds")
            if not cadence or age is None:
                continue
            cadence_seconds = bar_interval_seconds(str(cadence))
            if not cadence_seconds:
                continue
            if float(age) > FEED_STALE_MULTIPLIER * cadence_seconds:
                add(
                    "feed_stale",
                    "warn",
                    f"feature {name} is {_ago(float(age))} behind its {cadence} cadence",
                    since=entry.get("latest_timestamp"),
                    scope="data",
                )

    def read_failed() -> None:
        if contract not in {"freestyle_v1", "path_v1"}:
            return
        tick = store.read_json(job_id, FREESTYLE_LAST_TICK_PATH, default=None)
        if not isinstance(tick, dict):
            return
        for line in tick.get("logs") or []:
            text = str(line)
            if any(marker in text for marker in READ_FAILURE_MARKERS):
                add(
                    "read_failed",
                    "warn",
                    f"the last tick could not read a venue: {text[:200]}",
                    since=tick.get("ts"),
                    scope="data",
                )
                return
        if tick.get("mode") == "paper" and tick.get("unpapered_actions"):
            add(
                "unpapered_actions",
                "info",
                f"{len(tick['unpapered_actions'])} ctx.custom action(s) were skipped in paper mode",
                since=tick.get("ts"),
                scope="script",
            )

    def dataset_fetch() -> None:
        fetch = scorecard.get("dataset_fetch") or {}
        if fetch.get("status") == "failed":
            add(
                "dataset_fetch_failed",
                "warn",
                "the dataset fetch failed; backtest and evolution wait on it",
                since=fetch.get("finished_at"),
                scope="data",
                fix="re-run the dataset fetch (readout refresh=true)",
            )

    def risk() -> None:
        pending = [
            flag
            for flag in risk_flags or []
            if flag.get("severity") in {"block", "warn"}
            and not flag.get("acknowledged")
        ]
        if not pending:
            return
        blocking = any(flag.get("severity") == "block" for flag in pending)
        codes = ", ".join(str(flag.get("code")) for flag in pending)
        add(
            "risk_flags_unacknowledged",
            "block" if blocking else "warn",
            f"unacknowledged risk flags: {codes}",
            scope="risk",
            fix=str(
                pending[0].get("fix")
                or "acknowledge each warn flag with a memo before going live"
            ),
        )

    def checklist() -> None:
        if not launch.get("launched") or not isinstance(launch_checklist, dict):
            return
        if launch_checklist.get("ok") is False:
            reasons = launch_checklist.get("reasons") or []
            add(
                "launch_checklist_failing",
                "warn",
                str(reasons[0]) if reasons else "the launch checklist no longer passes",
                since=launch_checklist.get("checked_at"),
                scope="launch",
                fix="re-run validate and launch",
            )

    def apply_stalled() -> None:
        for proposal in proposals or []:
            application = proposal.get("application") or {}
            status = application.get("status")
            if status not in {"queued", "applying"}:
                continue
            started = _parse_iso(
                application.get("started_at") or application.get("requested_at")
            )
            if started and (now - started).total_seconds() > APPLY_STALL_SECONDS:
                add(
                    "apply_stalled",
                    "warn",
                    f"proposal {proposal.get('proposal_id')} has been {status} for "
                    f"{_ago((now - started).total_seconds())}",
                    since=application.get("started_at")
                    or application.get("requested_at"),
                    scope="proposal",
                    fix="the application watchdog retries; if it stays stuck, recover-stalled-applications",
                )

    def apply_reverted() -> None:
        for proposal in proposals or []:
            reverted = (proposal.get("application") or {}).get("reverted") or {}
            at = _parse_iso(reverted.get("ts"))
            if not at or (now - at).total_seconds() > APPLY_REVERT_WINDOW_SECONDS:
                continue
            add(
                "apply_reverted",
                "warn",
                f"approved proposal {proposal.get('proposal_id')} had been "
                f"applied and was reverted by the pipeline: {reverted.get('reason')}",
                since=reverted.get("ts"),
                scope="proposal",
                fix="the job runs without that change; re-stage it if still approved, otherwise propose it fresh for review",
                ref="owner_attention:apply_reverted",
            )

    def not_launched() -> None:
        if contract in LIFECYCLE_CONTRACTS and not launch.get("launched"):
            add(
                "not_launched",
                "info",
                "not launched yet",
                scope="launch",
                fix="run the launch checklist and launch in paper",
            )

    detectors: tuple[Callable[[], None], ...] = (
        runner_unreachable,
        script_loop,
        tick_failed,
        tick_overdue,
        agent_wake,
        revision_drift,
        mode_mismatch,
        halted,
        feed_stale,
        read_failed,
        dataset_fetch,
        risk,
        checklist,
        apply_stalled,
        apply_reverted,
        not_launched,
    )
    for detector in detectors:
        try:
            detector()
        except Exception:  # noqa: BLE001 — one bad artifact must not hide the rest
            continue
    issues.sort(
        key=lambda item: (
            SEVERITY_RANK.get(str(item.get("severity")), 9),
            -(
                _parse_iso(item.get("since")) or datetime.min.replace(tzinfo=UTC)
            ).timestamp(),
        )
    )
    return issues


def health_payload(
    store: JobStore,
    job_id: str,
    job: Any,
    *,
    runner_states: dict[str, Any],
    reports: dict[str, Any],
    scorecard: dict[str, Any] | None,
    features: list[dict[str, Any]] | None,
    risk_flags: list[dict[str, Any]] | None,
    launch: dict[str, Any] | None,
    launch_checklist: dict[str, Any] | None,
    halt: dict[str, Any] | None,
    proposals: list[dict[str, Any]] | None,
    workspace_revision: str | None,
) -> dict[str, Any]:
    """``{"heartbeat": ..., "issues": [...]}``; never raises."""
    heartbeat: dict[str, Any] | None
    try:
        heartbeat = build_heartbeat(
            store,
            job_id,
            job,
            runner_states=runner_states,
            reports=reports,
            launch=launch,
            halt=halt,
            workspace_revision=workspace_revision,
        )
    except Exception:  # noqa: BLE001
        heartbeat = None
    try:
        issues = build_issues(
            store,
            job_id,
            job,
            heartbeat=heartbeat,
            scorecard=scorecard,
            features=features,
            risk_flags=risk_flags,
            launch_checklist=launch_checklist,
            proposals=proposals,
        )
    except Exception:  # noqa: BLE001
        issues = []
    return {"heartbeat": heartbeat, "issues": issues}


__all__ = [
    "FREESTYLE_LAST_TICK_PATH",
    "ISSUE_CODES",
    "build_heartbeat",
    "build_issues",
    "health_payload",
    "sdk_version",
]
