from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import time
from pathlib import Path
from typing import Any

from wayfinder_paths.mcp.preview import build_run_script_preview
from wayfinder_paths.mcp.state.profile_store import WalletProfileStore
from wayfinder_paths.mcp.utils import (
    err,
    find_wallet_by_label,
    ok,
    repo_root,
)


def _runs_root() -> Path:
    candidate = (os.getenv("WAYFINDER_RUNS_DIR") or ".wayfinder_runs").strip()
    p = Path(candidate)
    if not p.is_absolute():
        p = repo_root() / p
    return p.resolve(strict=False)


def _resolve_script_path(script_path: str) -> tuple[bool, Path | dict[str, Any]]:
    raw = str(script_path).strip()
    if not raw:
        return False, {"code": "invalid_request", "message": "script_path is required"}

    p = Path(raw)
    if not p.is_absolute():
        p = repo_root() / p
    resolved = p.resolve(strict=False)

    runs_root = _runs_root()
    try:
        resolved.relative_to(runs_root)
    except ValueError:
        return (
            False,
            {
                "code": "invalid_request",
                "message": "script_path must be inside the local runs directory",
                "details": {
                    "runs_root": str(runs_root),
                    "script_path": str(resolved),
                },
            },
        )

    if not resolved.exists():
        return (
            False,
            {
                "code": "not_found",
                "message": "Script file not found",
                "details": {"script_path": str(resolved)},
            },
        )

    if resolved.suffix.lower() != ".py":
        return (
            False,
            {
                "code": "invalid_request",
                "message": "Only .py scripts are supported",
                "details": {"script_path": str(resolved)},
            },
        )

    return True, resolved


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _truncate(text: str, *, max_chars: int = 20_000) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


def _with_repo_pythonpath(env: dict[str, str]) -> dict[str, str]:
    root = str(repo_root())
    cur = env.get("PYTHONPATH", "").strip()
    env_out = dict(env)
    env_out["PYTHONPATH"] = f"{root}{os.pathsep}{cur}" if cur else root
    return env_out


_PROTOCOL_PATTERNS: list[tuple[str, str]] = [
    ("moonwell", "moonwell"),
    ("hyperliquid", "hyperliquid"),
    ("hyperlend", "hyperlend"),
    ("pendle", "pendle"),
    ("boros", "boros"),
    ("brap", "brap"),
    ("swap", "brap"),
]


def _infer_protocol_from_script(script_path: Path) -> str | None:
    name = script_path.stem.lower()
    for pattern, protocol in _PROTOCOL_PATTERNS:
        if pattern in name:
            return protocol
    return None


def _annotate_script_run(
    *,
    script_path: str,
    status: str,
    wallet_label: str | None = None,
    protocol: str | None = None,
) -> None:
    # Only annotates if wallet_label is provided - we must know the actual wallet
    if not protocol:
        return

    if not wallet_label:
        return

    wallet = find_wallet_by_label(wallet_label)
    if not wallet:
        return

    address = wallet.get("address")
    if not address:
        return

    store = WalletProfileStore.default()
    store.annotate_safe(
        address=address,
        label=wallet_label,
        protocol=protocol,
        action="run_script",
        tool="run_script",
        status=status,
        details={"script_path": script_path},
    )


async def run_script(
    *,
    script_path: str,
    args: list[str] | None = None,
    timeout_s: int = 600,
    env: dict[str, str] | None = None,
    force: bool = False,
    wallet_label: str | None = None,
) -> dict[str, Any]:
    ok_path, resolved_or_error = _resolve_script_path(script_path)
    if not ok_path:
        payload = resolved_or_error if isinstance(resolved_or_error, dict) else {}
        return err(
            payload.get("code") or "invalid_request",
            payload.get("message") or "Invalid script_path",
            payload.get("details"),
        )
    assert isinstance(resolved_or_error, Path)
    script = resolved_or_error

    root = repo_root()
    display_path = str(script)
    try:
        display_path = str(script.relative_to(root))
    except ValueError:
        pass

    args_list: list[str] = []
    if args:
        args_list = [str(a) for a in args if str(a).strip()]

    script_sha = None
    try:
        script_sha = _sha256_file(script)
    except OSError:
        pass

    tool_input = {
        "script_path": display_path,
        "args": args_list,
        "timeout_s": int(timeout_s),
        "env": env or {},
        "script_sha256": script_sha,
    }
    preview_obj = build_run_script_preview(tool_input)
    preview_text = str(preview_obj.get("summary") or "").strip()

    try:
        timeout = max(1, int(timeout_s))
    except (TypeError, ValueError):
        timeout = 600

    base_env = os.environ.copy()
    if env:
        base_env.update({str(k): str(v) for k, v in env.items()})
    exec_env = _with_repo_pythonpath(base_env)

    start = time.time()
    timed_out = False

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        *args_list,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(root),
        env=exec_env,
    )

    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        timed_out = True
        proc.kill()
        stdout_b, stderr_b = await proc.communicate()

    duration_s = time.time() - start
    stdout = _truncate((stdout_b or b"").decode("utf-8", errors="replace"))
    stderr = _truncate((stderr_b or b"").decode("utf-8", errors="replace"))

    exit_code = int(proc.returncode or 0)
    status = "timeout" if timed_out else ("completed" if exit_code == 0 else "failed")

    response = ok(
        {
            "status": status,
            "script_path": display_path,
            "script_sha256": script_sha,
            "args": args_list,
            "timeout_s": timeout,
            "duration_s": duration_s,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "preview": preview_text,
        }
    )

    inferred_protocol = _infer_protocol_from_script(script)
    if inferred_protocol and wallet_label:
        _annotate_script_run(
            script_path=display_path,
            status=status,
            wallet_label=wallet_label,
            protocol=inferred_protocol,
        )

    return response
