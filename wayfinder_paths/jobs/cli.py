from __future__ import annotations

import json
from typing import Any

import click

from wayfinder_paths.jobs.compiler import JobCompiler
from wayfinder_paths.jobs.models import AgentMode, WayfinderJob
from wayfinder_paths.jobs.runner_bridge import RunnerBridge
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.sync import snapshot_job, sync_all_jobs
from wayfinder_paths.jobs.worker import run_job_worker


def _echo_json(data: Any) -> None:
    click.echo(json.dumps(data, indent=2, default=str))


@click.group(name="job", help="High-level Wayfinder jobs: script loop + optional agent loop.")
def job_cli() -> None:
    pass


@job_cli.command(name="create", help="Create or update a high-level Wayfinder job.")
@click.argument("job_id")
@click.option("--name", default=None)
@click.option("--goal", default="")
@click.option("--script", default=None, help="Script entrypoint for the deterministic loop.")
@click.option("--interval", "interval_seconds", type=int, default=None)
@click.option("--cron", "cron_expr", default=None)
@click.option("--timezone", default="UTC", show_default=True)
@click.option("--timeout", "timeout_seconds", type=int, default=120, show_default=True)
@click.option(
    "--agent-mode",
    type=click.Choice(["off", "monitor", "improve", "decide"]),
    default="off",
    show_default=True,
)
@click.option("--agent-wake", "agent_wake_seconds", type=int, default=None)
@click.option("--no-compile", is_flag=True, default=False)
def create_cmd(
    job_id: str,
    name: str | None,
    goal: str,
    script: str | None,
    interval_seconds: int | None,
    cron_expr: str | None,
    timezone: str,
    timeout_seconds: int,
    agent_mode: AgentMode,
    agent_wake_seconds: int | None,
    no_compile: bool,
) -> None:
    if not script and agent_mode == "off":
        raise click.UsageError("Provide --script and/or --agent-mode monitor|improve")
    if script and not interval_seconds and not cron_expr:
        raise click.UsageError("Script jobs require --interval or --cron")

    store = JobStore()
    job = WayfinderJob.new(
        job_id,
        name=name,
        goal=goal,
        script=script,
        interval_seconds=interval_seconds,
        cron_expr=cron_expr,
        timezone=timezone,
        timeout_seconds=timeout_seconds,
        agent_mode=agent_mode,
        agent_wake_seconds=agent_wake_seconds,
    )
    path = store.save(job)
    result: dict[str, Any] = {"job": job.to_dict(), "job_yaml": str(path)}
    if not no_compile:
        result["compile"] = JobCompiler(store=store).compile(job)
        sync_all_jobs(store=store)
    _echo_json({"ok": True, "result": result})


@job_cli.command(name="list", help="List high-level Wayfinder jobs.")
def list_cmd() -> None:
    store = JobStore()
    _echo_json({"ok": True, "result": [snapshot_job(job.id, store=store) for job in store.list_jobs()]})


@job_cli.command(name="status", help="Show a high-level job snapshot.")
@click.argument("job_id")
def status_cmd(job_id: str) -> None:
    store = JobStore()
    _echo_json({"ok": True, "result": snapshot_job(job_id, store=store)})


@job_cli.command(name="report", help="Show a compact terminal report for a job.")
@click.argument("job_id")
def report_cmd(job_id: str) -> None:
    store = JobStore()
    snap = snapshot_job(job_id, store=store)
    job = snap["job"]
    scorecard = snap.get("scorecard") or {}
    proposals = snap.get("proposals") or []
    click.echo(f"{job['name']} — {job['id']}")
    click.echo("")
    click.echo(f"Goal: {job.get('goal') or 'not recorded'}")
    click.echo(f"Health: {scorecard.get('health', 'unknown')}")
    click.echo(f"Script loop: {'on' if job['script_loop'].get('enabled') else 'off'}")
    click.echo(f"Agent loop: {job['agent_loop'].get('mode', 'off')}")
    click.echo(f"Pending proposals: {sum(1 for p in proposals if p.get('status') == 'pending')}")
    latest_summary = scorecard.get("last_agent_summary")
    if latest_summary:
        click.echo("")
        click.echo(f"Latest agent check: {latest_summary}")


@job_cli.group(name="agent", help="Control a job's agent loop.")
def agent_group() -> None:
    pass


@agent_group.command(name="set-mode", help="Set agent mode and recompile runner links.")
@click.argument("job_id")
@click.argument("mode", type=click.Choice(["off", "monitor", "improve", "decide"]))
@click.option("--wake", "wake_seconds", type=int, default=None)
def agent_set_mode_cmd(job_id: str, mode: AgentMode, wake_seconds: int | None) -> None:
    store = JobStore()
    job = store.load(job_id)
    job.agent_loop.mode = mode
    job.agent_loop.enabled = mode != "off"
    if wake_seconds is not None:
        job.agent_loop.wake_interval_seconds = wake_seconds
    store.save(job)
    result = JobCompiler(store=store).compile(job)
    sync_all_jobs(store=store)
    _echo_json({"ok": True, "result": result})


@agent_group.command(name="review-now", help="Run a headless worker review immediately.")
@click.argument("job_id")
@click.option("--mode", type=click.Choice(["monitor", "improve", "decide"]), default=None)
def review_now_cmd(job_id: str, mode: str | None) -> None:
    result = run_job_worker(job_id, mode=mode or "monitor")
    _echo_json({"ok": True, "result": result})


@job_cli.command(name="proposals", help="List proposals for a job.")
@click.argument("job_id")
def proposals_cmd(job_id: str) -> None:
    store = JobStore()
    _echo_json({"ok": True, "result": store.proposals(job_id)})


@job_cli.command(name="approve", help="Approve a pending proposal.")
@click.argument("job_id")
@click.argument("proposal_id")
def approve_cmd(job_id: str, proposal_id: str) -> None:
    store = JobStore()
    proposal = store.set_proposal_status(job_id, proposal_id, "approved")
    store.append_journal(job_id, {"type": "proposal_approved", "proposal_id": proposal_id})
    sync_all_jobs(store=store)
    _echo_json({"ok": True, "result": proposal})


@job_cli.command(name="reject", help="Reject a pending proposal.")
@click.argument("job_id")
@click.argument("proposal_id")
def reject_cmd(job_id: str, proposal_id: str) -> None:
    store = JobStore()
    proposal = store.set_proposal_status(job_id, proposal_id, "rejected")
    store.append_journal(job_id, {"type": "proposal_rejected", "proposal_id": proposal_id})
    sync_all_jobs(store=store)
    _echo_json({"ok": True, "result": proposal})


@job_cli.command(name="pause", help="Pause a job's runner loops.")
@click.argument("job_id")
def pause_cmd(job_id: str) -> None:
    store = JobStore()
    job = store.load(job_id)
    bridge = RunnerBridge(repo_root=store.repo_root)
    responses = []
    if job.script_loop.enabled:
        responses.append(bridge.pause(job.script_loop.runner_job_name))
    if job.agent_loop.enabled:
        responses.append(bridge.pause(job.agent_loop.runner_job_name))
    sync_all_jobs(store=store)
    _echo_json({"ok": True, "result": responses})


@job_cli.command(name="resume", help="Resume a job's runner loops.")
@click.argument("job_id")
def resume_cmd(job_id: str) -> None:
    store = JobStore()
    job = store.load(job_id)
    bridge = RunnerBridge(repo_root=store.repo_root)
    responses = []
    if job.script_loop.enabled:
        responses.append(bridge.resume(job.script_loop.runner_job_name))
    if job.agent_loop.enabled:
        responses.append(bridge.resume(job.agent_loop.runner_job_name))
    sync_all_jobs(store=store)
    _echo_json({"ok": True, "result": responses})


@job_cli.command(name="delete", help="Delete runner links for a high-level job.")
@click.argument("job_id")
def delete_cmd(job_id: str) -> None:
    store = JobStore()
    job = store.load(job_id)
    bridge = RunnerBridge(repo_root=store.repo_root)
    responses = []
    if job.script_loop.runner_job_name:
        responses.append(bridge.delete(job.script_loop.runner_job_name))
    if job.agent_loop.runner_job_name:
        responses.append(bridge.delete(job.agent_loop.runner_job_name))
    store.refresh_scorecard(job_id, {"health": "unknown", "deleted": True})
    sync_all_jobs(store=store)
    _echo_json({"ok": True, "result": responses})
