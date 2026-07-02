from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
from typing import Any

from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.runner_bridge import RunnerBridge
from wayfinder_paths.jobs.store import JobStore


class JobCompiler:
    def __init__(self, *, store: JobStore | None = None) -> None:
        self.store = store or JobStore()
        self.bridge = RunnerBridge(repo_root=self.store.repo_root)

    def compile(
        self, job: WayfinderJob, *, start_daemon: bool = True
    ) -> dict[str, Any]:
        root = self.store.init_layout(job)
        wrappers = self._write_wrappers(job, root)
        # runner_links.json always carries "jobs" — init_layout seeds it,
        # compile rewrites it.
        previous_links = (
            self.store.read_json(job.id, "runner_links.json", default={}) or {}
        )
        job_env = self._job_env(job, root)
        if start_daemon:
            self.bridge.ensure_started()

        linked: list[dict[str, Any]] = []
        if job.script_loop.enabled:
            if not wrappers.get("script"):
                raise ValueError(
                    "script loop is enabled but no script wrapper was generated"
                )
            resp = self.bridge.add_or_update_script_job(
                name=job.script_loop.runner_job_name,
                script_path=wrappers["script"],
                interval_seconds=job.script_loop.interval_seconds,
                cron_expr=job.script_loop.cron_expr,
                timezone=job.script_loop.timezone,
                timeout_seconds=job.script_loop.timeout_seconds,
                env=job_env,
            )
            linked.append(
                {
                    "loop": "script",
                    "runner_job_name": job.script_loop.runner_job_name,
                    "response": resp,
                }
            )
        elif job.script_loop.runner_job_name and any(
            item["loop"] == "script" for item in previous_links["jobs"]
        ):
            resp = self.bridge.delete(job.script_loop.runner_job_name)
            linked.append(
                {
                    "loop": "script",
                    "runner_job_name": job.script_loop.runner_job_name,
                    "response": resp,
                }
            )

        if job.agent_loop.enabled and job.agent_loop.mode != "off":
            resp = self.bridge.add_or_update_script_job(
                name=job.agent_loop.runner_job_name,
                script_path=wrappers["agent"],
                interval_seconds=job.agent_loop.wake_interval_seconds,
                cron_expr=job.agent_loop.cron_expr,
                timezone=job.agent_loop.timezone,
                timeout_seconds=job.agent_loop.timeout_seconds,
                env={
                    **job_env,
                    "WAYFINDER_JOB_AGENT_MODE": job.agent_loop.mode,
                },
            )
            linked.append(
                {
                    "loop": "agent",
                    "runner_job_name": job.agent_loop.runner_job_name,
                    "response": resp,
                }
            )
        elif job.agent_loop.runner_job_name and any(
            item["loop"] == "agent" for item in previous_links["jobs"]
        ):
            resp = self.bridge.delete(job.agent_loop.runner_job_name)
            linked.append(
                {
                    "loop": "agent",
                    "runner_job_name": job.agent_loop.runner_job_name,
                    "response": resp,
                }
            )

        payload = {"job_id": job.id, "jobs": linked}
        self.store.write_json(job.id, "runner_links.json", payload)
        self.store.append_journal(job.id, {"type": "compiled", "runner_links": linked})
        return payload

    def _write_wrappers(self, job: WayfinderJob, root: Path) -> dict[str, str]:
        safe_module_name = job.id.replace("-", "_")
        wrapper_paths: dict[str, str] = {}

        if job.script_loop.enabled and job.script_loop.entrypoint:
            entrypoint = self.store.resolve_script_entrypoint(job.id, job.to_dict())
            if entrypoint is None:
                raise ValueError("script loop is enabled but entrypoint is missing")
            script_wrapper = self.store.runs_jobs_dir / f"{safe_module_name}_script.py"
            if job.execution_contract == "jobs_v1":
                # SDK-owned tick driver: the strategy module only exposes
                # decide()/build_strategy(); the driver does data fetch,
                # reconcile, order routing, and telemetry.
                script_wrapper.write_text(
                    dedent(
                        f"""
                        from __future__ import annotations

                        import sys
                        from pathlib import Path

                        from wayfinder_paths.jobs.execution.driver import run_scheduled_tick

                        JOB_DIR = Path({str(root)!r})

                        if __name__ == "__main__":
                            sys.path.insert(0, str(JOB_DIR / "workspace"))
                            payload = run_scheduled_tick(JOB_DIR)
                            raise SystemExit(0 if payload.get("ok") else 1)
                        """
                    ).lstrip(),
                    encoding="utf-8",
                )
            else:
                script_wrapper.write_text(
                    dedent(
                        f"""
                        from __future__ import annotations

                        import runpy
                        import sys
                        from pathlib import Path

                        ENTRYPOINT = Path({str(entrypoint)!r})
                        JOB_DIR = Path({str(root)!r})

                        if __name__ == "__main__":
                            sys.path.insert(0, str(JOB_DIR / "workspace"))
                            sys.argv = [str(ENTRYPOINT), *sys.argv[1:]]
                            runpy.run_path(str(ENTRYPOINT), run_name="__main__")
                        """
                    ).lstrip(),
                    encoding="utf-8",
                )
            wrapper_paths["script"] = str(
                script_wrapper.relative_to(self.store.repo_root)
            )

        agent_wrapper = self.store.runs_jobs_dir / f"{safe_module_name}_agent.py"
        agent_wrapper.write_text(
            dedent(
                f"""
                from __future__ import annotations

                import os

                from wayfinder_paths.jobs.worker import run_job_worker

                if __name__ == "__main__":
                    run_job_worker(
                        job_id={job.id!r},
                        mode=os.environ.get("WAYFINDER_JOB_AGENT_MODE") or {job.agent_loop.mode!r},
                    )
                """
            ).lstrip(),
            encoding="utf-8",
        )
        wrapper_paths["agent"] = str(agent_wrapper.relative_to(self.store.repo_root))
        return wrapper_paths

    def _job_env(self, job: WayfinderJob, root: Path) -> dict[str, str]:
        env = {
            "WAYFINDER_HIGH_LEVEL_JOB_ID": job.id,
            "WAYFINDER_JOB_DIR": str(root),
            "WAYFINDER_FORWARD_DIR": str(root / "results" / "forward"),
            "WAYFINDER_JOB_MODE": str(job.script_loop.mode or "paper"),
            "WAYFINDER_JOB_REVISION": str(job.versioning.get("active_revision") or ""),
        }
        spec_path = root / "execution_spec.json"
        if job.execution_spec:
            self.store.write_json(job.id, "execution_spec.json", job.execution_spec)
        if spec_path.exists():
            env["WAYFINDER_EXECUTION_SPEC"] = str(spec_path)
        return env


def compile_job(job_id: str, *, start_daemon: bool = True) -> dict[str, Any]:
    store = JobStore()
    job = store.load(job_id)
    result = JobCompiler(store=store).compile(job, start_daemon=start_daemon)
    return json.loads(json.dumps(result, default=str))
