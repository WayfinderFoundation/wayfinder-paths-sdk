#!/usr/bin/env python3
# ruff: noqa: E402
"""Evaluate the Wayfinder job-worker prompt cache contract.

Default mode is deterministic and CI-safe: it builds a temporary Wayfinder job
bundle, mutates dynamic state, and asserts that only the dynamic prompt section
changes. Pass ``--live`` to additionally make a small real OpenCode model call.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from wayfinder_paths.core.config import get_api_key
from wayfinder_paths.jobs.models import JOB_WORKER_AGENT_NAME, WayfinderJob
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.worker import (
    DYNAMIC_CONTEXT_MARKER,
    STABLE_PREFIX_END_MARKER,
    _build_worker_prompt_sections,
)

DEFAULT_OUTPUT_DIR = ".wayfinder_runs/evals/job_worker_cache"
DEFAULT_MODEL = "wayfinder/deepseek-v4-pro"
DEFAULT_OPENCODE = str(Path.home() / ".opencode" / "bin" / "opencode")
DEFAULT_OPENCODE_DB = str(Path.home() / ".local" / "share" / "opencode" / "opencode.db")
LIVE_SENTINEL = "JOB_CACHE_LIVE_OK"


def repo_root() -> Path:
    return REPO_ROOT


def _check(name: str, passed: bool) -> dict[str, Any]:
    return {"name": name, "passed": passed}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def run_deterministic_eval(output_dir: Path) -> dict[str, Any]:
    workspace = output_dir / "workspace"
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    store = JobStore(repo_root=workspace)
    job = WayfinderJob.new(
        "job-cache-eval",
        goal="Monitor cache behavior for the job worker.",
        script="workspace/src/loop.py",
        agent_mode="monitor",
    )
    store.save(job)
    (store.job_dir(job.id) / "memory.md").write_text(
        "# Job Cache Eval Memory\n\n"
        "Goal:\n"
        "Keep durable instructions stable across ordinary wakeups.\n\n"
        "Known lessons:\n"
        "- Only material strategy lessons belong in stable memory.\n",
        encoding="utf-8",
    )
    store.write_json(
        job.id,
        "memory.json",
        {
            "job_id": job.id,
            "updated_at": "volatile-timestamp",
            "lessons": ["stable durable lesson"],
            "constraints": ["never place live orders in monitor mode"],
        },
    )

    first = _build_worker_prompt_sections(
        store=store,
        job_id=job.id,
        mode="monitor",
        snapshot={"job": job.to_dict(), "scorecard": {"health": "green"}},
    )

    volatile_job = job.to_dict()
    volatile_job["created_at"] = "2040-01-01T00:00:00+00:00"
    volatile_job["updated_at"] = "2040-01-01T00:00:01+00:00"
    volatile = _build_worker_prompt_sections(
        store=store,
        job_id=job.id,
        mode="monitor",
        snapshot={"job": volatile_job, "scorecard": {"health": "green"}},
    )

    store.append_journal(
        job.id,
        {
            "type": "script_run",
            "summary": "dynamic price state changed",
            "ts": "dynamic-timestamp",
        },
    )
    store.write_json(
        job.id,
        "reports/monitor/latest.json",
        {
            "created_at": "dynamic-report-timestamp",
            "summary": "latest run changed",
        },
    )
    second = _build_worker_prompt_sections(
        store=store,
        job_id=job.id,
        mode="monitor",
        snapshot={
            "job": volatile_job,
            "scorecard": {"health": "yellow"},
            "reports": {"monitor": {"summary": "latest run changed"}},
        },
    )

    (store.job_dir(job.id) / "memory.md").write_text(
        "# Job Cache Eval Memory\n\n"
        "Goal:\n"
        "Keep durable instructions stable across ordinary wakeups.\n\n"
        "Known lessons:\n"
        "- Only material strategy lessons belong in stable memory.\n"
        "- Durable rule changed after user approval.\n",
        encoding="utf-8",
    )
    durable_change = _build_worker_prompt_sections(
        store=store,
        job_id=job.id,
        mode="monitor",
        snapshot={"job": volatile_job, "scorecard": {"health": "yellow"}},
    )

    checks = [
        _check(
            "stable_marker_precedes_dynamic_marker",
            first["prompt"].index(STABLE_PREFIX_END_MARKER)
            < first["prompt"].index(DYNAMIC_CONTEXT_MARKER),
        ),
        _check(
            "volatile_job_timestamps_do_not_change_stable_hash",
            first["stable_prefix_hash"] == volatile["stable_prefix_hash"],
        ),
        _check(
            "dynamic_state_does_not_change_stable_hash",
            first["stable_prefix_hash"] == second["stable_prefix_hash"],
        ),
        _check(
            "dynamic_state_changes_dynamic_hash",
            first["dynamic_context_hash"] != second["dynamic_context_hash"],
        ),
        _check(
            "recent_journal_is_dynamic_only",
            "dynamic price state changed" not in second["stable_prefix"]
            and "dynamic price state changed" in second["dynamic_context"],
        ),
        _check(
            "durable_memory_change_changes_stable_hash",
            second["stable_prefix_hash"] != durable_change["stable_prefix_hash"],
        ),
    ]
    report = {
        "status": "passed" if all(check["passed"] for check in checks) else "failed",
        "checks": checks,
        "artifacts": {
            "workspace": str(workspace),
            "stable_prefix_hash": first["stable_prefix_hash"],
            "dynamic_context_hash": second["dynamic_context_hash"],
            "stable_prefix_chars": len(first["stable_prefix"]),
            "dynamic_context_chars": len(second["dynamic_context"]),
        },
    }
    _write_json(output_dir / "deterministic_report.json", report)
    return report


def _session_ids_from_json_events(text: str) -> list[str]:
    session_ids: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        session_id = event.get("sessionID")
        part = event.get("part")
        match part:
            case dict() if not session_id:
                session_id = part.get("sessionID")
        match session_id:
            case str() if session_id not in seen:
                seen.add(session_id)
                session_ids.append(session_id)
    return session_ids


def _harvest_session_text(db_path: Path, session_ids: list[str]) -> str:
    if not db_path.exists() or not session_ids:
        return ""
    try:
        con = sqlite3.connect(db_path)
    except Exception:
        return ""
    texts: list[str] = []
    try:
        for session_id in session_ids:
            rows = con.execute(
                """SELECT json_extract(p.data,'$.text')
                   FROM part p JOIN message m ON p.message_id = m.id
                   WHERE m.session_id=?
                     AND json_extract(p.data,'$.type')='text'
                   ORDER BY m.time_created""",
                (session_id,),
            ).fetchall()
            texts.extend(str(row[0]) for row in rows if row and row[0])
    except Exception:
        return ""
    finally:
        con.close()
    return "\n".join(texts)


def run_live_eval(
    *,
    repo_root_path: Path,
    output_dir: Path,
    model: str,
    opencode_bin: str,
    timeout_seconds: int,
    db_path: Path = Path(DEFAULT_OPENCODE_DB),
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    if not env.get("WAYFINDER_API_KEY"):
        api_key = get_api_key()
        if api_key:
            env["WAYFINDER_API_KEY"] = api_key

    prompt = (
        "Live eval for the Wayfinder job-worker cache contract. Do not call tools. "
        f"Reply with exactly `{LIVE_SENTINEL}` and nothing else."
    )
    command = [
        opencode_bin,
        "run",
        "--agent",
        JOB_WORKER_AGENT_NAME,
        "-m",
        model,
        "--format",
        "json",
        "--title",
        f"eval/job-worker-cache/{int(time.time())}",
        prompt,
    ]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=repo_root_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        returncode: int | None = completed.returncode
        error = None
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        returncode = None
        error = f"timeout after {timeout_seconds}s"

    (output_dir / "live_stdout.txt").write_text(stdout, encoding="utf-8")
    (output_dir / "live_stderr.txt").write_text(stderr, encoding="utf-8")
    session_ids = _session_ids_from_json_events(stdout)
    harvested = _harvest_session_text(db_path, session_ids)
    (output_dir / "live_harvest.txt").write_text(harvested, encoding="utf-8")
    observed_text = f"{stdout}\n{stderr}\n{harvested}"
    passed = returncode == 0 and LIVE_SENTINEL in observed_text
    report = {
        "status": "passed" if passed else "failed",
        "passed": passed,
        "returncode": returncode,
        "duration_seconds": round(time.monotonic() - started, 3),
        "model": model,
        "command": [
            opencode_bin,
            "run",
            "--agent",
            JOB_WORKER_AGENT_NAME,
            "-m",
            model,
            "...",
        ],
        "sentinel": LIVE_SENTINEL,
        "session_ids": session_ids,
        "error": error,
        "stdout_path": str(output_dir / "live_stdout.txt"),
        "stderr_path": str(output_dir / "live_stderr.txt"),
        "harvest_path": str(output_dir / "live_harvest.txt"),
    }
    _write_json(output_dir / "live_report.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--live", action="store_true", help="Also run a real OpenCode call."
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--opencode-bin", default=DEFAULT_OPENCODE)
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args(argv)

    output_dir = (repo_root() / args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    deterministic = run_deterministic_eval(output_dir)
    result: dict[str, Any] = {"deterministic": deterministic}
    status = deterministic["status"]
    if args.live:
        live = run_live_eval(
            repo_root_path=repo_root(),
            output_dir=output_dir,
            model=args.model,
            opencode_bin=args.opencode_bin,
            timeout_seconds=args.timeout,
        )
        result["live"] = live
        status = (
            "passed" if status == "passed" and live["status"] == "passed" else "failed"
        )

    result["status"] = status
    _write_json(output_dir / "latest.json", result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
