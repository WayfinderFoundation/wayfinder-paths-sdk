from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.compiler import JobCompiler
from wayfinder_paths.jobs.launch import hold_job
from wayfinder_paths.jobs.models import WayfinderJob, normalize_agent_mode, safe_job_id
from wayfinder_paths.jobs.notify_policy import default_notifications
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.sync import sync_all_jobs

DEFAULT_TIMEOUT_S = 300
SCRIPT_JOB_TRIGGERS: tuple[str, ...] = (
    "script_failure",
    "risk_halt",
    "runner_loop_gap",
)


def create_freestyle_job(
    job_id: str,
    *,
    name: str | None = None,
    goal: str = "",
    script_source: str | None = None,
    script_path: str | Path | None = None,
    interval_seconds: int | None = None,
    cron_expr: str | None = None,
    timezone: str = "UTC",
    timeout_seconds: int = DEFAULT_TIMEOUT_S,
    agent_mode: str = "monitor",
    store: JobStore | None = None,
    compile_job: bool = True,
    initializer_session_id: str | None = None,
    execution_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Materialize a freestyle job, paused: the tick module lands under
    ``workspace/src`` (the only revision-hashed home), the job compiles into
    the runner and its loops stay paused until ``launch``."""
    if not script_source and script_path is None:
        raise ValueError("create_freestyle_job needs script_source or script_path")
    if not interval_seconds and not cron_expr:
        raise ValueError("freestyle jobs need interval_seconds or cron_expr")
    store = store or JobStore()
    jid = safe_job_id(job_id)
    module_name = f"{jid.replace('-', '_')}.py"
    job = WayfinderJob.new(
        jid,
        name=name,
        goal=goal,
        script=f"workspace/src/{module_name}",
        interval_seconds=interval_seconds,
        cron_expr=cron_expr,
        timezone=timezone,
        timeout_seconds=int(timeout_seconds or DEFAULT_TIMEOUT_S),
        agent_mode=normalize_agent_mode(agent_mode),
        execution_contract="freestyle_v1",
        initializer_session_id=initializer_session_id,
        source={
            "kind": "freestyle",
            "origin": f"path:{script_path}"
            if script_path is not None and not script_source
            else "inline",
        },
    )
    if execution_params:
        job.execution_params.update(dict(execution_params))
    job.agent_loop.triggers = list(SCRIPT_JOB_TRIGGERS)
    job.reporting = {**job.reporting, "notify": default_notifications(job)}
    root = store.init_layout(job)
    target = root / "workspace" / "src" / module_name
    if script_source:
        target.write_text(script_source, encoding="utf-8")
    else:
        source = Path(str(script_path))
        if not source.is_file():
            raise FileNotFoundError(f"script not found: {source}")
        shutil.copy2(source, target)
    store.create_job(job)
    result: dict[str, Any] = {
        "job": job.to_dict(),
        "job_yaml": str(root / "job.yaml"),
        "script_entrypoint": str(target),
        "hint": (
            "the module must define tick(ctx); trade only through ctx.act. Run "
            "validate_job (static rules + a three-tick paper dry run), read the "
            "launch_checklist, then launch in paper."
        ),
    }
    if compile_job:
        result["compile"] = JobCompiler(store=store).compile(job)
        result["hold"] = hold_job(job.id, store=store)
        sync_all_jobs(store=store)
    return result
