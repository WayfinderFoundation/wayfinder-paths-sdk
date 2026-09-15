"""Validation ladder for freestyle jobs: static rules that keep every trade
behind ``ctx.act`` plus a sandboxed dry run (three paper ticks on stub marks)
that proves the script runs mechanically before anything is launched."""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from wayfinder_paths.jobs.execution.validation import (
    FORBIDDEN_ORDER_PATTERNS,
    _code_only_text,
    entrypoint_inside_workspace_check,
    report_from_checks,
)
from wayfinder_paths.jobs.freestyle.runtime import (
    DRY_RUN_RESULT_PATH,
    JOB_RESULT_MARKER,
)
from wayfinder_paths.jobs.gating import compute_workspace_revision
from wayfinder_paths.jobs.store import JobStore

EXTRA_FORBIDDEN_PATTERNS = (
    "swap_from_quote(",
    "onchain_swap(",
    "onchain_send(",
    "redeem_positions(",
    ".cancel_order(",
    ".place_order(",
    "contract_execute(",
)
FORBIDDEN_IMPORT_PREFIXES = (
    "wayfinder_paths.mcp.tools.hyperliquid",
    "wayfinder_paths.mcp.tools.polymarket",
    "wayfinder_paths.mcp.tools.onchain",
    "wayfinder_paths.mcp.tools.contracts",
    "wayfinder_paths.jobs.execution.hyperliquid",
    "wayfinder_paths.jobs.execution.polymarket",
    "wayfinder_paths.adapters.brap_adapter",
    "wayfinder_paths.adapters.hyperliquid_adapter",
    "wayfinder_paths.adapters.polymarket_adapter",
)
SLEEP_PATTERNS = ("time.sleep(", "asyncio.sleep(", "while True")
DRY_RUN_TICKS = 3
DEFAULT_DRY_RUN_TIMEOUT_S = 300
MAX_TIMEOUT_S = 3600


def static_checks(script_path: Path) -> list[dict[str, Any]]:
    """Rules a freestyle script must satisfy before it runs anywhere."""
    checks: list[dict[str, Any]] = []
    source = script_path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(script_path))
        checks.append({"name": "py_compile", "passed": True})
    except SyntaxError as exc:
        checks.append({"name": "py_compile", "passed": False, "error": str(exc)})
        return checks
    tick = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "tick"
        ),
        None,
    )
    checks.append(
        {
            "name": "tick_entrypoint_present",
            "passed": tick is not None and len(tick.args.args) >= 1,
            "hint": "define `def tick(ctx):` at module level",
        }
    )
    checks.append(
        {
            "name": "tick_is_sync",
            "passed": not isinstance(tick, ast.AsyncFunctionDef),
            "hint": "tick(ctx) must be a plain function; ctx.quote/ctx.act are synchronous",
        }
    )
    # _code_only_text re-joins tokens with spaces ("time . sleep ("), so every
    # pattern is matched against the whitespace-free form of the code.
    code = _code_only_text(source).replace(" ", "")
    hits = [
        pattern
        for pattern in (*FORBIDDEN_ORDER_PATTERNS, *EXTRA_FORBIDDEN_PATTERNS)
        if pattern.replace(" ", "") in code
    ]
    imports = _forbidden_imports(tree)
    checks.append(
        {
            "name": "no_direct_venue_writes",
            "passed": not hits and not imports,
            "patterns": hits,
            "imports": imports,
            "hint": (
                "freestyle scripts trade only through ctx.act — that is the one "
                "seam paper mode can intercept"
            ),
        }
    )
    sleeps = [pattern for pattern in SLEEP_PATTERNS if pattern.replace(" ", "") in code]
    checks.append(
        {
            "name": "no_sleep_loops",
            "passed": not sleeps,
            "patterns": sleeps,
            "hint": "a tick returns quickly; the runner schedules the next one",
        }
    )
    checks.append(
        {
            "name": "no_forward_recorder",
            "passed": "ForwardRecorder" not in code,
            "hint": "the runtime records orders, fills and runs for you",
        }
    )
    checks.append(
        {
            "name": "state_via_ctx",
            "passed": "monitor_state" not in code,
            "blocking": False,
            "hint": "durable state belongs in ctx.state; it persists between ticks",
        }
    )
    return checks


def run_dry_run(
    root: Path,
    *,
    job_id: str,
    repo_root: Path,
    timeout_s: int,
    ticks: int = DRY_RUN_TICKS,
    marks: dict[str, float] | None = None,
    entrypoint: Path | None = None,
    extra_sys_path: list[str] | None = None,
) -> dict[str, Any]:
    """Three paper ticks in a subprocess with stub marks and an isolated
    forward directory. Nothing here touches the job's real ledger, state or
    the network."""
    dry_dir = root / "reports" / "validation" / "dryrun"
    if dry_dir.exists():
        shutil.rmtree(dry_dir)
    dry_dir.mkdir(parents=True, exist_ok=True)
    workspace = root / "workspace"
    env = {
        **os.environ,
        "WAYFINDER_JOB_MODE": "paper",
        "WAYFINDER_DRY_RUN": "1",
        "WAYFINDER_JOB_DIR": str(root),
        "WAYFINDER_FORWARD_DIR": str(dry_dir / "forward"),
        "WAYFINDER_HIGH_LEVEL_JOB_ID": job_id,
        "WAYFINDER_KV_NAMESPACE": f"{job_id}-dryrun",
        "WAYFINDER_JOB_REVISION": "",
        # The SDK package root first: the job store's repo root is not always
        # the SDK checkout (tests, boxes with a vault-side .wayfinder).
        "PYTHONPATH": os.pathsep.join(
            [
                str(_sdk_root()),
                str(repo_root),
                str(workspace),
                os.environ.get("PYTHONPATH", ""),
            ]
        ).rstrip(os.pathsep),
    }
    cmd = [
        sys.executable,
        "-m",
        "wayfinder_paths.jobs.freestyle.runtime",
        "--job-dir",
        str(root),
        "--dry-run",
        "--ticks",
        str(ticks),
    ]
    if marks:
        cmd += ["--marks", json.dumps(marks)]
    if entrypoint is not None:
        cmd += ["--entrypoint", str(entrypoint)]
    for extra in extra_sys_path or []:
        cmd += ["--sys-path", str(extra)]
    outcome: dict[str, Any] = {"ok": False, "ticks": ticks, "timeout_s": timeout_s}
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(repo_root),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        outcome.update(
            {
                "timed_out": True,
                "error": f"dry run exceeded {timeout_s}s",
                "stdout_tail": (exc.stdout or "")[-2000:]
                if isinstance(exc.stdout, str)
                else "",
            }
        )
        return outcome
    (dry_dir / "stdout.log").write_text(completed.stdout or "", encoding="utf-8")
    (dry_dir / "stderr.log").write_text(completed.stderr or "", encoding="utf-8")
    outcome["exit_code"] = completed.returncode
    outcome["marker"] = _last_marker(completed.stdout or "")
    result = _read_json(root / DRY_RUN_RESULT_PATH) or {}
    outcome["result"] = {
        key: result.get(key)
        for key in (
            "ok",
            "status",
            "error",
            "actions",
            "fills",
            "guard_events",
            "marks",
            "funding",
            "token_values",
            "yields",
            "equity",
            "positions",
            "logs",
            "unpapered_actions",
            "venues_used",
            "spec",
            "notifications",
            "dry_run",
        )
    }
    outcome["ok"] = completed.returncode == 0 and bool(result.get("ok"))
    if not outcome["ok"]:
        outcome["error"] = (
            result.get("error") or (completed.stderr or "")[-1500:] or "dry run failed"
        )
    return outcome


def validate_freestyle_job(
    job_id: str,
    *,
    candidate_dir: str | Path | None = None,
    store: JobStore | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    store = store or JobStore()
    root = Path(candidate_dir) if candidate_dir else store.job_dir(job_id)
    job_yaml_path = root / "job.yaml"
    checks: list[dict[str, Any]] = [
        {
            "name": "job_yaml_exists",
            "passed": job_yaml_path.exists(),
            "path": str(job_yaml_path),
        }
    ]
    job_data: dict[str, Any] = {}
    if job_yaml_path.exists():
        loaded = yaml.safe_load(job_yaml_path.read_text(encoding="utf-8")) or {}
        match loaded:
            case dict():
                job_data = loaded
                checks.append({"name": "job_yaml_parse", "passed": True})
            case _:
                checks.append({"name": "job_yaml_parse", "passed": False})
    checks.append(
        {
            "name": "execution_contract_freestyle_v1",
            "passed": str(job_data.get("execution_contract")) == "freestyle_v1",
        }
    )
    script_loop = job_data.get("script_loop") or {}
    timeout_s = int(script_loop.get("timeout_seconds") or 0)
    checks.append(
        {
            "name": "timeout_declared",
            "passed": 0 < timeout_s <= MAX_TIMEOUT_S,
            "blocking": False,
            "timeout_seconds": timeout_s,
            "hint": f"script_loop.timeout_seconds must be within (0, {MAX_TIMEOUT_S}]",
        }
    )
    script_path = store.resolve_script_entrypoint(
        job_id, job_data, candidate_dir=root if candidate_dir else None
    )
    checks.append(
        {
            "name": "execution_script_exists",
            "passed": bool(script_path and script_path.exists()),
            "path": str(script_path) if script_path else None,
        }
    )
    checks.append(entrypoint_inside_workspace_check(root, script_path))
    freestyle: dict[str, Any] = {}
    if script_path and script_path.exists():
        checks.extend(static_checks(script_path))
        static_ok = all(c["passed"] for c in checks if c.get("blocking") is not False)
        if dry_run and static_ok:
            outcome = run_dry_run(
                root,
                job_id=job_id,
                repo_root=store.repo_root,
                timeout_s=timeout_s
                if 0 < timeout_s <= MAX_TIMEOUT_S
                else DEFAULT_DRY_RUN_TIMEOUT_S,
                marks=_validation_marks(job_data),
            )
            result = outcome.get("result") or {}
            checks.append(
                {
                    "name": "dry_run_ok",
                    "passed": bool(outcome.get("ok")),
                    "error": outcome.get("error"),
                    "exit_code": outcome.get("exit_code"),
                    "timed_out": bool(outcome.get("timed_out")),
                    "ticks": outcome.get("ticks"),
                }
            )
            refused = [
                action
                for action in result.get("actions") or []
                if action.get("status") == "refused"
            ]
            unsupported = [
                action
                for action in refused
                if "not supported" in str(action.get("reason") or "")
            ]
            checks.append(
                {
                    "name": "dry_run_venues_supported",
                    "passed": not unsupported,
                    "refused": [a.get("reason") for a in unsupported],
                }
            )
            checks.append(
                {
                    "name": "dry_run_actions_papered",
                    "passed": not result.get("unpapered_actions"),
                    "blocking": False,
                    "unpapered_actions": result.get("unpapered_actions") or [],
                    "hint": "ctx.custom calls cannot be papered; they are skipped in paper mode",
                }
            )
            checks.append(
                {
                    "name": "dry_run_no_refusals",
                    "passed": not refused,
                    "blocking": False,
                    "refused": [a.get("reason") for a in refused],
                }
            )
            freestyle = {
                "spec": result.get("spec") or {},
                "dry_run": {
                    "ok": outcome.get("ok"),
                    "ticks": outcome.get("ticks"),
                    "actions": result.get("actions") or [],
                    "fills": result.get("fills") or [],
                    "intents": [
                        a.get("intent")
                        for a in result.get("actions") or []
                        if a.get("intent")
                    ],
                    "guard_events": result.get("guard_events") or [],
                    "equity": result.get("equity"),
                    "positions": result.get("positions") or {},
                    "unpapered_actions": result.get("unpapered_actions") or [],
                    "venues_used": result.get("venues_used") or [],
                    "funding": result.get("funding") or {},
                    "token_values": result.get("token_values") or {},
                    "yields": result.get("yields") or {},
                    "marks": result.get("marks") or {},
                    "notifications": result.get("notifications") or [],
                    "logs": result.get("logs") or [],
                    "error": outcome.get("error"),
                },
            }
        elif dry_run:
            checks.append(
                {
                    "name": "dry_run_ok",
                    "passed": False,
                    "skipped": True,
                    "error": "static checks failed; dry run not attempted",
                }
            )
    report = report_from_checks(checks, strict=False)
    report["revision"] = compute_workspace_revision(root)
    report["kind"] = "freestyle_v1"
    report["paper_capable"] = True
    report["freestyle"] = freestyle
    if not candidate_dir:
        store.write_json(job_id, "reports/validation/latest.json", report)
    return report


def _sdk_root() -> Path:
    import wayfinder_paths

    return Path(wayfinder_paths.__file__).resolve().parents[1]


def _forbidden_imports(tree: ast.AST) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            if any(
                name == prefix or name.startswith(prefix + ".")
                for prefix in FORBIDDEN_IMPORT_PREFIXES
            ):
                found.append(name)
    return found


def _last_marker(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if stripped.startswith(JOB_RESULT_MARKER):
            try:
                return json.loads(stripped[len(JOB_RESULT_MARKER) :])
            except ValueError:
                return {
                    "summary": stripped[:500],
                    "severity": "info",
                    "parseError": True,
                }
    return None


def _validation_marks(job_data: dict[str, Any]) -> dict[str, float] | None:
    raw = ((job_data.get("execution_params") or {}).get("freestyle") or {}).get(
        "validation_marks"
    )
    if not raw:
        return None
    return {str(k): float(v) for k, v in dict(raw).items()}


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
