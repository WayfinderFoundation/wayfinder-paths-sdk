from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from wayfinder_paths import __version__
from wayfinder_paths.runner.constants import (
    JOB_STATUS_ACTIVE,
    JOB_STATUS_PAUSED,
    JOB_TYPE_SCRIPT,
    JOB_TYPE_STRATEGY,
    RUN_STATUS_FAILED,
    RUN_STATUS_OK,
    RUN_STATUS_TIMEOUT,
)
from wayfinder_paths.runner.db import RunnerDB
from wayfinder_paths.runner.paths import RunnerPaths
from wayfinder_paths.runner.script_resolver import resolve_script_path


def _utc_epoch_s() -> int:
    return int(time.time())


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


def _kill_process_group(pid: int, *, sig: int) -> None:
    try:
        os.killpg(int(pid), sig)
    except ProcessLookupError:
        return
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"Failed to kill process group {pid}: {exc}")


def _clamp_int(value: Any, *, min_value: int, max_value: int, default: int) -> int:
    try:
        i = int(value)
    except (TypeError, ValueError):
        return int(default)
    return max(int(min_value), min(int(max_value), i))


def _with_repo_pythonpath(env: dict[str, str], *, repo_root: Path) -> dict[str, str]:
    root = str(repo_root)
    cur = env.get("PYTHONPATH", "").strip()
    env_out = dict(env)
    env_out["PYTHONPATH"] = f"{root}{os.pathsep}{cur}" if cur else root
    return env_out


@dataclass
class RunningProcess:
    run_id: int
    job_id: int
    job_name: str
    started_at: int
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
        self._started_at = _utc_epoch_s()
        self._last_tick_at: int | None = None

        self._lock = threading.Lock()
        self._shutdown = threading.Event()
        self._running: dict[int, RunningProcess] = {}
        self._running_by_job: dict[int, int] = {}

        self._control = None
        self._daemon_log_sink_id: int | None = None

    @property
    def paths(self) -> RunnerPaths:
        return self._paths

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

        from wayfinder_paths.runner.control import RunnerControlServer

        self._control = RunnerControlServer(
            sock_path=self._paths.sock_path, daemon=self
        )
        self._control.start()
        logger.info(
            f"Runner daemon v{__version__} listening on {self._paths.sock_path}"
        )

        self._install_signal_handlers()
        try:
            self._loop()
        finally:
            self._shutdown.set()
            self._stop_control()
            self._shutdown_running_processes()
            self._db.close()
            if self._daemon_log_sink_id is not None:
                try:
                    logger.remove(self._daemon_log_sink_id)
                except Exception:  # noqa: BLE001
                    pass

    def stop(self) -> None:
        self._shutdown.set()

    def _install_signal_handlers(self) -> None:
        def _handle(sig, _frame):  # type: ignore[no-untyped-def]
            logger.info(f"Received signal {sig}; shutting down")
            self.stop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handle)
            except Exception:  # noqa: BLE001
                continue

    def _stop_control(self) -> None:
        if self._control is not None:
            self._control.stop()
            self._control = None

    def _loop(self) -> None:
        while not self._shutdown.is_set():
            tick_started = time.monotonic()
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"Runner tick error: {exc}")
            elapsed = time.monotonic() - tick_started
            sleep_s = max(0.0, self._tick_seconds - elapsed)
            time.sleep(sleep_s)

    def tick(self) -> None:
        now = _utc_epoch_s()
        with self._lock:
            self._last_tick_at = now
            self._reap(now=now)

            for job in self._db.due_jobs(now=now):
                self._maybe_start_job(job=job, now=now, reason="schedule")

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
                        f"Run {run_id} timed out after {rp.timeout_seconds}s; killing process group"
                    )
                    _kill_process_group(proc.pid, sig=signal.SIGTERM)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        _kill_process_group(proc.pid, sig=signal.SIGKILL)
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            pass
                    self._finish_run(
                        rp,
                        finished_at=now,
                        status=RUN_STATUS_TIMEOUT,
                        exit_code=proc.returncode,
                        error_text=f"timeout after {rp.timeout_seconds}s",
                    )
                continue

            status = RUN_STATUS_OK if int(exit_code) == 0 else RUN_STATUS_FAILED
            error_text = None
            if status != RUN_STATUS_OK:
                error_text = _tail_text(rp.log_path) or f"exit_code={exit_code}"
            self._finish_run(
                rp,
                finished_at=now,
                status=status,
                exit_code=int(exit_code),
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
        self._db.finish_run(
            run_id=rp.run_id,
            finished_at=finished_at,
            status=status,
            exit_code=exit_code,
            summary={"error": error_text} if error_text else None,
        )

        if status == RUN_STATUS_OK:
            self._db.record_job_success(job_id=rp.job_id, ok_at=finished_at)
        else:
            msg = error_text or status
            failures, job_status = self._db.record_job_failure(
                job_id=rp.job_id,
                error_text=msg,
                max_failures=self._max_failures,
            )
            if job_status != JOB_STATUS_ACTIVE:
                logger.error(
                    f"Job {rp.job_name} entered {job_status} after {failures} failures"
                )

        self._running.pop(rp.run_id, None)
        self._running_by_job[rp.job_id] = max(
            0, self._running_by_job.get(rp.job_id, 1) - 1
        )

    def _shutdown_running_processes(self) -> None:
        with self._lock:
            running = list(self._running.values())
        if not running:
            return
        logger.info(f"Shutting down {len(running)} running worker(s)")
        for rp in running:
            try:
                _kill_process_group(rp.popen.pid, sig=signal.SIGTERM)
            except Exception:  # noqa: BLE001
                continue

    def _maybe_start_job(
        self, *, job: dict[str, Any], now: int, reason: str
    ) -> int | None:
        if len(self._running) >= self._max_workers:
            return None
        job_id = int(job["id"])
        job_name = str(job["name"])
        if self._running_by_job.get(job_id, 0) >= 1:
            return None

        interval = int(job.get("interval_seconds") or 0)
        next_run_at = now + max(1, interval)
        self._db.set_job_last_run(job_id=job_id, last_run_at=now)
        self._db.set_next_run_at(job_id=job_id, next_run_at=next_run_at)

        job_dir = self._paths.logs_dir / _safe_job_dirname(job_name)
        job_dir.mkdir(parents=True, exist_ok=True)

        run_id = self._db.create_run(
            job_id=job_id,
            started_at=now,
        )
        log_path = job_dir / f"{run_id}.log"
        self._db.update_run_log_path(run_id=run_id, log_path=str(log_path))

        payload = job.get("payload") or {}
        timeout_seconds = (
            payload.get("timeout_seconds") or payload.get("timeout") or None
        )
        if timeout_seconds is None:
            timeout_seconds = self._default_timeout_seconds

        env = os.environ.copy()
        env.update(
            {
                "WAYFINDER_RUN_ID": str(run_id),
                "WAYFINDER_JOB_ID": str(job_id),
                "WAYFINDER_JOB_NAME": str(job_name),
                "WAYFINDER_RUNNER_DIR": str(self._paths.runner_dir),
                "WAYFINDER_KV_NAMESPACE": str(job_name),
                "WAYFINDER_RUNNER_REASON": str(reason),
            }
        )
        payload_env = payload.get("env")
        if isinstance(payload_env, dict):
            env.update({str(k): str(v) for k, v in payload_env.items()})
        if payload.get("wallet_label"):
            env["WAYFINDER_WALLET_LABEL"] = str(payload.get("wallet_label"))
        env = _with_repo_pythonpath(env, repo_root=self._paths.repo_root)

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
                status=RUN_STATUS_FAILED,
                exit_code=None,
                summary={"error": err_text},
            )
            self._db.record_job_failure(
                job_id=job_id,
                error_text=err_text,
                max_failures=self._max_failures,
            )
            return None
        logger.info(f"Starting job {job_name} (run_id={run_id})")

        try:
            with log_path.open("ab", buffering=0) as log_f:
                log_f.write(
                    (
                        f"[runner] job={job_name} run_id={run_id} started_at={now} reason={reason}\n"
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
                status=RUN_STATUS_FAILED,
                exit_code=None,
                summary={"error": err_text},
            )
            self._db.record_job_failure(
                job_id=job_id,
                error_text=err_text,
                max_failures=self._max_failures,
            )
            return None

        self._db.update_run_pid(run_id=run_id, pid=int(popen.pid))

        self._running[run_id] = RunningProcess(
            run_id=run_id,
            job_id=job_id,
            job_name=job_name,
            started_at=now,
            timeout_seconds=int(timeout_seconds) if timeout_seconds else None,
            popen=popen,
            log_path=log_path,
        )
        self._running_by_job[job_id] = self._running_by_job.get(job_id, 0) + 1
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
            if args is None:
                args = []
            if not isinstance(args, list):
                raise ValueError("payload.args must be a list of strings")
            arg_list = [str(a) for a in args if str(a).strip()]
            return [sys.executable, str(script), *arg_list]

        raise ValueError(f"Unsupported job type: {job_type}")

    # Control-plane methods (called by runnerctl over the local socket)
    def ctl_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ok": True,
                "result": {
                    "version": __version__,
                    "pid": os.getpid(),
                    "ppid": os.getppid(),
                    "started_at": self._started_at,
                    "uptime_s": max(0, _utc_epoch_s() - self._started_at),
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
        if name is None:
            return {"ok": False, "error": "name is required"}
        name = str(name).strip()
        if not name:
            return {"ok": False, "error": "name is required"}

        lim = _clamp_int(limit, min_value=1, max_value=500, default=50)
        try:
            job, _ = self._db.get_job(name=name)
        except KeyError as exc:
            return {"ok": False, "error": str(exc)}

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

        tbytes = _clamp_int(tail_bytes, min_value=200, max_value=200_000, default=4000)

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
        interval_seconds: int,
    ) -> dict[str, Any]:
        if name is None:
            return {"ok": False, "error": "name is required"}
        name = str(name).strip()
        if not name:
            return {"ok": False, "error": "name is required"}

        if interval_seconds is None:
            return {"ok": False, "error": "interval_seconds is required"}
        try:
            interval_i = int(interval_seconds)
        except (TypeError, ValueError):
            return {"ok": False, "error": "interval_seconds must be an integer"}
        if interval_i <= 0:
            return {"ok": False, "error": "interval_seconds must be > 0"}

        if job_type is None:
            return {"ok": False, "error": "type is required"}
        job_type = str(job_type).strip()
        if job_type not in {JOB_TYPE_STRATEGY, JOB_TYPE_SCRIPT}:
            return {"ok": False, "error": f"unsupported job type: {job_type}"}

        if not isinstance(payload, dict):
            return {"ok": False, "error": "payload must be an object"}

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
        try:
            job_id = self._db.add_job(
                name=name,
                job_type=job_type,
                payload=payload_norm,
                interval_seconds=interval_i,
                status=JOB_STATUS_ACTIVE,
                next_run_at=_utc_epoch_s(),
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "result": {"job_id": job_id, "name": name}}

    def ctl_update_job(
        self, *, name: str, payload: dict[str, Any] | None, interval_seconds: int | None
    ) -> dict[str, Any]:
        if payload is not None and not isinstance(payload, dict):
            return {"ok": False, "error": "payload must be an object"}
        try:
            self._db.update_job(
                name=name, payload=payload, interval_seconds=interval_seconds
            )
            return {"ok": True, "result": {"name": name}}
        except KeyError as exc:
            return {"ok": False, "error": str(exc)}

    def ctl_pause_job(self, *, name: str) -> dict[str, Any]:
        try:
            self._db.set_job_status(name=name, status=JOB_STATUS_PAUSED)
            return {"ok": True, "result": {"name": name, "status": JOB_STATUS_PAUSED}}
        except KeyError as exc:
            return {"ok": False, "error": str(exc)}

    def ctl_resume_job(self, *, name: str) -> dict[str, Any]:
        try:
            job, _ = self._db.get_job(name=name)
            self._db.set_job_status(name=name, status=JOB_STATUS_ACTIVE)
            self._db.set_next_run_at(job_id=job.id, next_run_at=_utc_epoch_s())
            return {"ok": True, "result": {"name": name, "status": JOB_STATUS_ACTIVE}}
        except KeyError as exc:
            return {"ok": False, "error": str(exc)}

    def ctl_run_once(self, *, name: str) -> dict[str, Any]:
        now = _utc_epoch_s()
        try:
            job, state = self._db.get_job(name=name)
        except KeyError as exc:
            return {"ok": False, "error": str(exc)}
        if state.status != JOB_STATUS_ACTIVE:
            return {"ok": False, "error": f"job is not ACTIVE (status={state.status})"}

        job_dict: dict[str, Any] = {
            "id": job.id,
            "name": job.name,
            "type": job.type,
            "payload": job.payload,
            "interval_seconds": job.interval_seconds,
        }
        with self._lock:
            run_id = self._maybe_start_job(job=job_dict, now=now, reason="run_once")
        if run_id is None:
            return {
                "ok": False,
                "error": "job could not be started (running or at capacity)",
            }
        return {"ok": True, "result": {"name": name, "run_id": run_id}}

    def ctl_delete_job(self, *, name: str) -> dict[str, Any]:
        try:
            job, _ = self._db.get_job(name=name)
        except KeyError as exc:
            return {"ok": False, "error": str(exc)}

        job_id = int(job.id)
        with self._lock:
            if self._running_by_job.get(job_id, 0) >= 1:
                return {"ok": False, "error": "job is currently running"}
            try:
                self._db.delete_job(name=str(name))
            except KeyError as exc:
                return {"ok": False, "error": str(exc)}
            self._running_by_job.pop(job_id, None)

        return {"ok": True, "result": {"name": str(name), "deleted": True}}
