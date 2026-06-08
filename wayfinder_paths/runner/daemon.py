from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from wayfinder_paths import __version__
from wayfinder_paths.core.clients.OpenCodeClient import OPENCODE_CLIENT
from wayfinder_paths.core.clients.ScheduledJobsClient import SCHEDULED_JOBS_CLIENT
from wayfinder_paths.core.config import is_opencode_instance
from wayfinder_paths.runner.constants import (
    JOB_TYPE_SCRIPT,
    JOB_TYPE_STRATEGY,
    JobStatus,
    RunStatus,
)
from wayfinder_paths.runner.control import RunnerControlServer
from wayfinder_paths.runner.db import RunnerDB
from wayfinder_paths.runner.paths import RunnerPaths
from wayfinder_paths.runner.schedule import (
    SCHEDULE_KIND_CRON,
    SCHEDULE_KIND_INTERVAL,
    next_run_after,
    normalize_schedule,
    schedule_from_job,
)
from wayfinder_paths.runner.script_resolver import resolve_script_path

JOB_RESULT_MARKER = "WAYFINDER_JOB_RESULT "
JOB_LOCK_TIMEOUT_SECONDS = 3
JOB_LOCK_BUSY_MSG = (
    "Runner Daemon lock is busy, no operations were completed, please try again later"
)
SESSION_ENV_KEYS = (
    "OPENCODE_SESSION_ID",
    "OPENCODE_SESSIONID",
)


def _safe_job_dirname(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in name)
    return cleaned.strip("_") or "job"


def _tail_text(path: Path, *, max_bytes: int = 4000) -> str | None:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - int(max_bytes))
            f.seek(start, os.SEEK_SET)
            data = f.read()
    except OSError:
        return None
    text = data.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    return text[-max_bytes:]


def _extract_job_result_event(
    path: Path, *, max_bytes: int = 64_000
) -> dict[str, Any] | None:
    text = _tail_text(path, max_bytes=max_bytes)
    if not text:
        return None
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not stripped.startswith(JOB_RESULT_MARKER):
            continue
        raw = stripped[len(JOB_RESULT_MARKER) :].strip()
        try:
            event = json.loads(raw)
        except ValueError:
            return {
                "summary": raw[:1000],
                "severity": "info",
                "parseError": True,
            }
        if isinstance(event, dict):
            return event
        return {
            "summary": str(event)[:1000],
            "severity": "info",
        }
    return None


def _kill_process_group(pid: int, *, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        return
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"Failed to kill process group {pid}: {exc}")


@dataclass
class RunningProcess:
    run_id: int
    job_id: int
    job_name: str
    started_at: int
    reason: str
    scheduled_for: int | None
    timeout_seconds: int | None
    popen: subprocess.Popen[bytes]
    log_path: Path


class RunnerDaemon:
    def __init__(
        self,
        *,
        paths: RunnerPaths,
        tick_seconds: float = 1.0,
        max_workers: int = 4,
        max_failures: int = 5,
        default_timeout_seconds: int = 20 * 60,
        log_level: str = "INFO",
    ) -> None:
        self._paths = paths
        self._tick_seconds = float(tick_seconds)
        self._max_workers = int(max_workers)
        self._max_failures = int(max_failures)
        self._default_timeout_seconds = int(default_timeout_seconds)
        self._log_level = str(log_level).upper()

        self._db = RunnerDB(paths.db_path)
        self._started_at = int(time.time())
        self._last_tick_at: int | None = None

        self._job_locks: dict[int, threading.Lock] = {}
        self._shutdown = threading.Event()
        self._running: dict[int, RunningProcess] = {}
        self._running_by_job: dict[int, int] = {}

        self._control = None
        self._daemon_log_sink_id: int | None = None

    def _lock_for_job(self, job_id: int) -> threading.Lock:
        lock = self._job_locks.get(job_id)
        if lock is None:
            lock = threading.Lock()
            self._job_locks[job_id] = lock
        return lock

    def start(self) -> None:
        self._paths.runner_dir.mkdir(parents=True, exist_ok=True)
        self._paths.logs_dir.mkdir(parents=True, exist_ok=True)

        daemon_log_path = self._paths.logs_dir / "wayfinder-daemon.log"
        try:
            self._daemon_log_sink_id = logger.add(
                str(daemon_log_path),
                level=self._log_level,
                rotation="10 MB",
                retention="7 days",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                f"Failed to configure daemon log file {daemon_log_path}: {exc}"
            )

        aborted = self._db.mark_stale_running_runs_aborted(note="runner restarted")
        if aborted:
            logger.warning(f"Marked {aborted} stale RUNNING runs as ABORTED")

        self._sync_to_backend_async()

        self._control = RunnerControlServer(
            sock_path=self._paths.sock_path, daemon=self
        )
        self._control.start()
        logger.info(
            f"Runner daemon v{__version__} listening on {self._paths.sock_path}"
        )

        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: self.stop())
        except ValueError:
            pass
        try:
            self._loop()
        finally:
            self._shutdown.set()
            self._control.stop()
            for rp in self._running.values():
                _kill_process_group(rp.popen.pid, sig=signal.SIGTERM)
            self._db._conn.close()
            if self._daemon_log_sink_id is not None:
                try:
                    logger.remove(self._daemon_log_sink_id)
                except Exception:  # noqa: BLE001
                    pass

    def stop(self) -> None:
        self._shutdown.set()

    def _loop(self) -> None:
        while not self._shutdown.is_set():
            tick_started = time.monotonic()
            self.tick()
            elapsed = time.monotonic() - tick_started
            time.sleep(max(0.0, self._tick_seconds - elapsed))

    def tick(self) -> None:
        try:
            now = int(time.time())
            self._last_tick_at = now
            self._reap(now=now)
            for job in self._db.due_jobs(now=now):
                self._maybe_start_job(job=job, now=now, reason="schedule")
        except Exception:  # noqa: BLE001
            logger.exception("Runner tick error")

    def _reap(self, *, now: int) -> None:
        for run_id, rp in list(self._running.items()):
            proc = rp.popen
            exit_code = proc.poll()
            if exit_code is None:
                if (
                    rp.timeout_seconds is not None
                    and now - rp.started_at > rp.timeout_seconds
                ):
                    logger.warning(
                        f"Run {run_id} timed out after {rp.timeout_seconds}s; killing"
                    )
                    _kill_process_group(proc.pid, sig=signal.SIGKILL)
                    self._finish_run(
                        rp,
                        finished_at=now,
                        status=RunStatus.TIMEOUT,
                        exit_code=proc.returncode,
                        error_text=f"timeout after {rp.timeout_seconds}s",
                    )
                continue

            status = RunStatus.OK if exit_code == 0 else RunStatus.FAILED
            error_text = None
            if status != RunStatus.OK:
                error_text = _tail_text(rp.log_path) or f"exit_code={exit_code}"
            self._finish_run(
                rp,
                finished_at=now,
                status=status,
                exit_code=exit_code,
                error_text=error_text,
            )

    def _finish_run(
        self,
        rp: RunningProcess,
        *,
        finished_at: int,
        status: str,
        exit_code: int | None,
        error_text: str | None,
    ) -> None:
        with self._lock_for_job(rp.job_id):
            self._db.finish_run(
                run_id=rp.run_id,
                finished_at=finished_at,
                status=status,
                exit_code=exit_code,
                summary={"error": error_text} if error_text else None,
            )

            if status == RunStatus.OK:
                self._db.record_job_success(job_id=rp.job_id, ok_at=finished_at)
            else:
                msg = error_text or status
                failures, job_status = self._db.record_job_failure(
                    job_id=rp.job_id,
                    error_text=msg,
                    max_failures=self._max_failures,
                )
                if job_status != JobStatus.ACTIVE:
                    logger.error(
                        f"Job {rp.job_name} entered {job_status} after {failures} failures"
                    )

            self._running.pop(rp.run_id, None)
            self._running_by_job[rp.job_id] = max(
                0, self._running_by_job.get(rp.job_id, 1) - 1
            )

        self._run_side_effect(
            f"notify-session-{rp.job_name}",
            lambda: self._notify_session(rp, status=status, error_text=error_text),
        )

        if is_opencode_instance():
            self._run_side_effect(
                f"report-run-{rp.job_name}",
                lambda: self._report_finished_run(
                    rp,
                    finished_at=finished_at,
                    status=status,
                    exit_code=exit_code,
                ),
            )

    def _run_side_effect(self, label: str, callback: Callable[[], None]) -> None:
        def _target() -> None:
            try:
                callback()
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"Runner side effect {label} failed: {exc}")

        thread = threading.Thread(
            target=_target,
            name=f"wayfinder-runner-{_safe_job_dirname(label)}",
            daemon=True,
        )
        thread.start()

    def _report_finished_run(
        self,
        rp: RunningProcess,
        *,
        finished_at: int,
        status: str,
        exit_code: int | None,
    ) -> None:
        log_output = ""
        try:
            log_output = rp.log_path.read_text(errors="replace")
        except Exception:  # noqa: BLE001
            pass
        SCHEDULED_JOBS_CLIENT.report_run(
            rp.job_name,
            {
                "run_id": rp.run_id,
                "status": status,
                "started_at": datetime.fromtimestamp(rp.started_at, tz=UTC).isoformat(),
                "finished_at": datetime.fromtimestamp(finished_at, tz=UTC).isoformat(),
                "exit_code": exit_code,
                "reason": rp.reason,
                "scheduled_for": rp.scheduled_for,
                "log_output": log_output,
            },
        )

    def _bind_runner_session_async(self, name: str) -> None:
        if not is_opencode_instance():
            return

        def _bind() -> None:
            session_id = OPENCODE_CLIENT.find_runner_session()
            if not session_id:
                return
            result = self._db.get_job(name=name)
            if not result:
                return
            job, _ = result
            payload = dict(job.payload or {})
            if payload.get("notify_session_id"):
                return
            payload["notify_session_id"] = session_id
            self._db.update_job(name=name, payload=payload, interval_seconds=None)
            logger.info(f"Auto-bound job {name} to session {session_id}")

        self._run_side_effect(f"bind-runner-session-{name}", _bind)

    def _sync_to_backend_async(self) -> None:
        if not is_opencode_instance():
            return

        def _sync() -> None:
            jobs = []
            for j in self._db.list_jobs():
                result = self._db.get_job(name=j["name"])
                if not result:
                    continue
                job, state = result
                jobs.append(
                    {
                        "job_name": job.name,
                        "job_type": job.type,
                        "status": state.status,
                        "interval_seconds": job.interval_seconds,
                        "schedule_kind": job.schedule_kind,
                        "cron_expr": job.cron_expr,
                        "timezone": job.timezone,
                        "payload": job.payload,
                    }
                )
            SCHEDULED_JOBS_CLIENT.bulk_sync(jobs)

        self._run_side_effect("bulk-sync", _sync)

    def _notify_session(
        self,
        running_process: RunningProcess,
        *,
        status: str,
        error_text: str | None,
    ) -> None:
        result = self._db.get_job(name=running_process.job_name)
        if not result:
            return
        job, _ = result
        session_id = job.payload.get("notify_session_id")

        if not session_id or not OPENCODE_CLIENT.healthy():
            return
        event = _extract_job_result_event(running_process.log_path)
        should_post_success = job.payload.get(
            "always_notify_session_on_job_completion"
        ) is True or (event is not None)
        if status == RunStatus.OK and not should_post_success:
            return
        message = _tail_text(running_process.log_path, max_bytes=4000) or "(no output)"
        if event is not None:
            message = str(
                event.get("message") or event.get("summary") or "Scheduled job event"
            )

        payload: dict[str, Any] = {
            "type": "job_result",
            "name": running_process.job_name,
            "status": status,
            "error": error_text,
            "message": message,
        }
        if event is not None:
            payload["event"] = event
        notification = json.dumps(payload)
        OPENCODE_CLIENT.send_message(session_id, notification)

    def _maybe_start_job(
        self,
        *,
        job: dict[str, Any],
        now: int,
        reason: str,
        advance_schedule: bool = True,
    ) -> int | None:
        if len(self._running) >= self._max_workers:
            return None
        job_id = job["id"]
        job_name = job["name"]
        scheduled_for = (
            int(job.get("next_run_at") or now) if reason == "schedule" else None
        )
        next_run_at = None
        if advance_schedule:
            next_run_at = next_run_after(schedule_from_job(job), now=now)

        job_dir = self._paths.logs_dir / _safe_job_dirname(job_name)
        job_dir.mkdir(parents=True, exist_ok=True)

        with self._lock_for_job(job_id):
            if self._running_by_job.get(job_id, 0) >= 1:
                return None
            self._running_by_job[job_id] = self._running_by_job.get(job_id, 0) + 1

        try:
            run_id = self._db.reserve_run(
                job_id=job_id,
                started_at=now,
                next_run_at=next_run_at,
                reason=reason,
                scheduled_for=scheduled_for,
            )
        except Exception:
            with self._lock_for_job(job_id):
                self._running_by_job[job_id] = max(
                    0, self._running_by_job.get(job_id, 1) - 1
                )
            raise
        log_path = job_dir / f"{run_id}.log"
        self._db.update_run_log_path(run_id=run_id, log_path=str(log_path))

        payload = job.get("payload") or {}
        timeout_val = payload.get("timeout_seconds", payload.get("timeout"))
        if timeout_val is None:
            timeout_seconds: int | None = self._default_timeout_seconds
        else:
            try:
                timeout_i = int(timeout_val)
            except (TypeError, ValueError):
                timeout_i = self._default_timeout_seconds
            timeout_seconds = None if timeout_i <= 0 else int(timeout_i)

        env = os.environ.copy()
        env.update(
            {
                "WAYFINDER_RUN_ID": str(run_id),
                "WAYFINDER_JOB_ID": str(job_id),
                "WAYFINDER_JOB_NAME": str(job_name),
                "WAYFINDER_RUNNER_DIR": str(self._paths.runner_dir),
                "WAYFINDER_KV_NAMESPACE": str(job_name),
                "WAYFINDER_RUNNER_REASON": str(reason),
                "WAYFINDER_SCHEDULED_FOR": ""
                if scheduled_for is None
                else str(scheduled_for),
            }
        )
        if payload.get("env"):
            env.update(payload["env"])
        if payload.get("wallet_label"):
            env["WAYFINDER_WALLET_LABEL"] = payload["wallet_label"]
        root = str(self._paths.repo_root)
        cur = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{root}{os.pathsep}{cur}" if cur else root

        try:
            cmd = self._build_worker_cmd(job=job)
        except Exception as exc:  # noqa: BLE001
            err_text = f"build worker cmd failed: {exc}"
            try:
                log_path.write_text(err_text + "\n", encoding="utf-8")
            except OSError:
                pass
            self._db.finish_run(
                run_id=run_id,
                finished_at=now,
                status=RunStatus.FAILED,
                exit_code=None,
                summary={"error": err_text},
            )
            self._db.record_job_failure(
                job_id=job_id,
                error_text=err_text,
                max_failures=self._max_failures,
            )
            with self._lock_for_job(job_id):
                self._running_by_job[job_id] = max(
                    0, self._running_by_job.get(job_id, 1) - 1
                )
            return None
        logger.info(f"Starting job {job_name} (run_id={run_id})")

        try:
            with log_path.open("ab", buffering=0) as log_f:
                log_f.write(
                    (
                        f"[runner] job={job_name} run_id={run_id} started_at={now} "
                        f"reason={reason} scheduled_for={scheduled_for}\n"
                    ).encode()
                )
                popen = subprocess.Popen(  # noqa: S603
                    cmd,
                    cwd=str(self._paths.repo_root),
                    env=env,
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except Exception as exc:  # noqa: BLE001
            err_text = f"spawn failed: {exc}"
            self._db.finish_run(
                run_id=run_id,
                finished_at=now,
                status=RunStatus.FAILED,
                exit_code=None,
                summary={"error": err_text},
            )
            self._db.record_job_failure(
                job_id=job_id,
                error_text=err_text,
                max_failures=self._max_failures,
            )
            with self._lock_for_job(job_id):
                self._running_by_job[job_id] = max(
                    0, self._running_by_job.get(job_id, 1) - 1
                )
            return None

        self._db.update_run_pid(run_id=run_id, pid=int(popen.pid))

        self._running[run_id] = RunningProcess(
            run_id=run_id,
            job_id=job_id,
            job_name=job_name,
            started_at=now,
            reason=reason,
            scheduled_for=scheduled_for,
            timeout_seconds=timeout_seconds,
            popen=popen,
            log_path=log_path,
        )
        return run_id

    def _build_worker_cmd(self, *, job: dict[str, Any]) -> list[str]:
        job_type = str(job.get("type") or "")
        payload: dict[str, Any] = dict(job.get("payload") or {})
        if job_type == JOB_TYPE_STRATEGY:
            strategy = str(payload.get("strategy") or "").strip()
            action = str(payload.get("action") or "update").strip()
            config_path = str(payload.get("config") or "config.json")
            wallet_label = payload.get("wallet_label") or payload.get("wallet") or None
            debug = bool(payload.get("debug") or False)

            cmd = [
                sys.executable,
                "-m",
                "wayfinder_paths.run_strategy",
                "--strategy",
                strategy,
                "--action",
                action,
                "--config",
                config_path,
            ]
            if wallet_label:
                cmd.extend(["--wallet-label", str(wallet_label)])
            if debug:
                cmd.append("--debug")
            return cmd

        if job_type == JOB_TYPE_SCRIPT:
            sp = (
                payload.get("script_path")
                or payload.get("script")
                or payload.get("path")
            )
            if not sp:
                raise ValueError("payload.script_path is required for script jobs")

            script = resolve_script_path(self._paths, str(sp))
            args = payload.get("args") or []
            arg_list = [a for a in args if a]
            return [sys.executable, str(script), *arg_list]

        raise ValueError(f"Unsupported job type: {job_type}")

    # Control-plane methods (called by runnerctl over the local socket)
    def ctl_status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "result": {
                "version": __version__,
                "pid": os.getpid(),
                "ppid": os.getppid(),
                "started_at": self._started_at,
                "uptime_s": max(0, int(time.time()) - self._started_at),
                "last_tick_at": self._last_tick_at,
                "repo_root": str(self._paths.repo_root),
                "runner_dir": str(self._paths.runner_dir),
                "db_path": str(self._paths.db_path),
                "sock_path": str(self._paths.sock_path),
                "running_workers": len(self._running),
                "max_workers": self._max_workers,
                "jobs": self._db.list_jobs(),
                "recent_runs": self._db.last_runs(limit=20),
            },
        }

    def ctl_shutdown(self) -> dict[str, Any]:
        self.stop()
        return {"ok": True, "result": {"shutdown": True}}

    def ctl_job_runs(self, *, name: str, limit: int | None = None) -> dict[str, Any]:
        if not name:
            return {"ok": False, "error": "name is required"}

        lim = limit or 50
        if lim < 1 or lim > 500:
            return {"ok": False, "error": "limit must be between 1 and 500"}
        result = self._db.get_job(name=name)
        if not result:
            return {"ok": False, "error": f"Job not found: {name}"}
        job, _ = result

        runs = self._db.runs_for_job(job_id=job.id, limit=lim)
        return {
            "ok": True,
            "result": {"name": job.name, "job_id": job.id, "runs": runs},
        }

    def ctl_run_report(
        self, *, run_id: int, tail_bytes: int | None = None
    ) -> dict[str, Any]:
        try:
            rid = int(run_id)
        except (TypeError, ValueError):
            return {"ok": False, "error": "run_id must be an integer"}

        tbytes = tail_bytes or 4000
        if tbytes < 200 or tbytes > 200_000:
            return {"ok": False, "error": "tail_bytes must be between 200 and 200000"}

        run = self._db.get_run(run_id=rid)
        if run is None:
            return {"ok": False, "error": f"run not found: {rid}"}

        duration_s = None
        if run.get("finished_at") is not None:
            duration_s = max(0, int(run["finished_at"]) - int(run["started_at"]))

        log_tail = None
        log_path_s = run.get("log_path")
        if log_path_s:
            try:
                log_path = Path(str(log_path_s)).resolve()
                logs_root = self._paths.logs_dir.resolve()
                if log_path.is_relative_to(logs_root):
                    log_tail = _tail_text(log_path, max_bytes=tbytes)
            except Exception:  # noqa: BLE001
                log_tail = None

        return {
            "ok": True,
            "result": {
                "run": run,
                "duration_s": duration_s,
                "log_tail": log_tail,
                "tail_bytes": tbytes,
            },
        }

    def ctl_add_job(
        self,
        *,
        name: str,
        job_type: str,
        payload: dict[str, Any],
        interval_seconds: int | None = None,
        cron_expr: str | None = None,
        timezone: str | None = None,
    ) -> dict[str, Any]:
        if not name:
            return {"ok": False, "error": "name is required"}

        try:
            schedule = normalize_schedule(
                interval_seconds=interval_seconds,
                cron_expr=cron_expr,
                timezone=timezone,
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

        if job_type not in {JOB_TYPE_STRATEGY, JOB_TYPE_SCRIPT}:
            return {"ok": False, "error": f"unsupported job type: {job_type}"}

        payload_norm: dict[str, Any] = dict(payload)
        if job_type == JOB_TYPE_STRATEGY:
            strategy = str(payload_norm.get("strategy") or "").strip()
            if not strategy:
                return {"ok": False, "error": "payload.strategy is required"}
        elif job_type == JOB_TYPE_SCRIPT:
            sp = (
                payload_norm.get("script_path")
                or payload_norm.get("script")
                or payload_norm.get("path")
            )
            if not sp:
                return {"ok": False, "error": "payload.script_path is required"}
            try:
                resolved = resolve_script_path(self._paths, str(sp))
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": str(exc)}
            try:
                rel = resolved.relative_to(self._paths.repo_root)
                payload_norm["script_path"] = str(rel)
            except ValueError:
                payload_norm["script_path"] = str(resolved)

            args = payload_norm.get("args")
            if args is not None and not isinstance(args, list):
                return {"ok": False, "error": "payload.args must be a list of strings"}
            env = payload_norm.get("env")
            if env is not None and not isinstance(env, dict):
                return {"ok": False, "error": "payload.env must be an object"}
        session_id = payload_norm.get("notify_session_id")
        if session_id is None:
            session_id = next(
                (os.environ[key] for key in SESSION_ENV_KEYS if os.environ.get(key)),
                None,
            )
        payload_norm["notify_session_id"] = session_id

        try:
            now = int(time.time())
            job_id = self._db.add_job(
                name=name,
                job_type=job_type,
                payload=payload_norm,
                interval_seconds=int(schedule.interval_seconds or 0),
                schedule_kind=schedule.kind,
                cron_expr=schedule.cron_expr,
                timezone=schedule.timezone,
                status=JobStatus.ACTIVE,
                next_run_at=now
                if schedule.kind == SCHEDULE_KIND_INTERVAL
                else next_run_after(schedule, now=now),
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        if session_id is None:
            self._bind_runner_session_async(name)
        self._sync_to_backend_async()
        return {"ok": True, "result": {"job_id": job_id, "name": name}}

    def ctl_update_job(
        self,
        *,
        name: str,
        payload: dict[str, Any] | None,
        interval_seconds: int | None = None,
        cron_expr: str | None = None,
        timezone: str | None = None,
    ) -> dict[str, Any]:
        schedule_kwargs: dict[str, Any] = {}
        if interval_seconds is not None or cron_expr is not None:
            try:
                schedule = normalize_schedule(
                    interval_seconds=interval_seconds,
                    cron_expr=cron_expr,
                    timezone=timezone,
                )
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": str(exc)}
            schedule_kwargs = {
                "interval_seconds": int(schedule.interval_seconds or 0),
                "schedule_kind": schedule.kind,
                "timezone": schedule.timezone,
            }
            if schedule.kind == SCHEDULE_KIND_CRON:
                schedule_kwargs["cron_expr"] = schedule.cron_expr
            else:
                schedule_kwargs["clear_cron_expr"] = True
        self._db.update_job(
            name=name,
            payload=payload,
            **schedule_kwargs,
        )
        if schedule_kwargs:
            result = self._db.get_job(name=name)
            if result:
                job, _ = result
                job_dict = {
                    "interval_seconds": job.interval_seconds,
                    "schedule_kind": job.schedule_kind,
                    "cron_expr": job.cron_expr,
                    "timezone": job.timezone,
                }
                next_run_at = int(time.time())
                if job.schedule_kind == SCHEDULE_KIND_CRON:
                    next_run_at = next_run_after(
                        schedule_from_job(job_dict), now=next_run_at
                    )
                self._db.set_next_run_at(job_id=job.id, next_run_at=next_run_at)
        self._sync_to_backend_async()
        return {"ok": True, "result": {"name": name}}

    def ctl_pause_job(self, *, name: str) -> dict[str, Any]:
        self._db.set_job_status(name=name, status=JobStatus.PAUSED)
        self._sync_to_backend_async()
        return {"ok": True, "result": {"name": name, "status": JobStatus.PAUSED}}

    def ctl_resume_job(self, *, name: str) -> dict[str, Any]:
        result = self._db.get_job(name=name)
        if not result:
            return {"ok": False, "error": f"Job not found: {name}"}
        job, _ = result
        self._db.set_job_status(name=name, status=JobStatus.ACTIVE)
        now = int(time.time())
        next_run_at = now
        if job.schedule_kind == SCHEDULE_KIND_CRON:
            next_run_at = next_run_after(
                schedule_from_job(
                    {
                        "schedule_kind": job.schedule_kind,
                        "cron_expr": job.cron_expr,
                        "timezone": job.timezone,
                        "interval_seconds": job.interval_seconds,
                    }
                ),
                now=now,
            )
        self._db.set_next_run_at(job_id=job.id, next_run_at=next_run_at)
        self._sync_to_backend_async()
        return {"ok": True, "result": {"name": name, "status": JobStatus.ACTIVE}}

    def ctl_stop_job(self, *, name: str, sig: str | None = None) -> dict[str, Any]:
        if not name:
            return {"ok": False, "error": "name is required"}

        sig_name = str(sig or "TERM").strip().upper()
        sig_val = signal.SIGTERM
        if sig_name == "KILL":
            sig_val = signal.SIGKILL
        elif sig_name == "INT":
            sig_val = signal.SIGINT
        elif sig_name != "TERM":
            return {"ok": False, "error": "sig must be one of: TERM, INT, KILL"}

        result = self._db.get_job(name=name)
        if not result:
            return {"ok": False, "error": f"Job not found: {name}"}
        job, _ = result

        lock = self._lock_for_job(job.id)
        if not lock.acquire(timeout=JOB_LOCK_TIMEOUT_SECONDS):
            return {"ok": False, "error": JOB_LOCK_BUSY_MSG}
        killed: list[dict[str, Any]] = []
        try:
            for run_id, rp in list(self._running.items()):
                if rp.job_id != job.id:
                    continue
                _kill_process_group(rp.popen.pid, sig=sig_val)
                killed.append({"run_id": run_id, "pid": rp.popen.pid})
        finally:
            lock.release()

        if not killed:
            return {"ok": False, "error": "job is not currently running"}

        return {
            "ok": True,
            "result": {"name": name, "signal": sig_name, "killed": killed},
        }

    def ctl_run_once(self, *, name: str) -> dict[str, Any]:
        now = int(time.time())
        result = self._db.get_job(name=name)
        if not result:
            return {"ok": False, "error": f"Job not found: {name}"}
        job, state = result
        if state.status != JobStatus.ACTIVE:
            return {"ok": False, "error": f"job is not ACTIVE (status={state.status})"}

        job_dict: dict[str, Any] = {
            "id": job.id,
            "name": job.name,
            "type": job.type,
            "payload": job.payload,
            "interval_seconds": job.interval_seconds,
            "schedule_kind": job.schedule_kind,
            "cron_expr": job.cron_expr,
            "timezone": job.timezone,
            "next_run_at": state.next_run_at,
        }
        run_id = self._maybe_start_job(
            job=job_dict, now=now, reason="run_once", advance_schedule=False
        )
        if run_id is None:
            return {
                "ok": False,
                "error": "job could not be started (running or at capacity)",
            }
        return {"ok": True, "result": {"name": name, "run_id": run_id}}

    def ctl_delete_job(self, *, name: str) -> dict[str, Any]:
        result = self._db.get_job(name=name)
        if not result:
            return {"ok": False, "error": f"Job not found: {name}"}
        job, _ = result

        lock = self._lock_for_job(job.id)
        if not lock.acquire(timeout=JOB_LOCK_TIMEOUT_SECONDS):
            return {"ok": False, "error": JOB_LOCK_BUSY_MSG}
        try:
            if self._running_by_job.get(job.id, 0) >= 1:
                return {"ok": False, "error": "job is currently running"}
            self._db.delete_job(name=name)
            self._running_by_job.pop(job.id, None)
            self._job_locks.pop(job.id, None)
        finally:
            lock.release()

        self._sync_to_backend_async()
        return {"ok": True, "result": {"name": name, "deleted": True}}
