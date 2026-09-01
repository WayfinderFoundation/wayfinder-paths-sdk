"""Agent lane: the full production stack as an optimizer, through the REAL
OpenCode harness — `opencode run --agent wayfinder-job-worker` with the same
agent definition, tool permissions, MCP toolset, and prompt path as live
wakes. This is the whole-stack measurement; everything else in ADAPTERS is a
baseline.

Per world: a sandbox job bundle is built with the world's development data
planted as the job's backtest dataset and the genome interpreter installed
as the workspace strategy (genome fields in execution_params — the agent's
normal propose flow mutates exactly the benchmark's search space). The agent
gets N intervene wakes; every candidate it evaluates or proposes is
harvested as lineage; its final applied/approved genome is the selection.
Tokens and calls are metered from the opencode session DB.

Run manually (never CI): model spend is real. Results feed the same
score_run/aggregate pipeline as every other lane.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.benchmarks.compiler import write_interpreter
from wayfinder_paths.jobs.benchmarks.grammar import Genome

DEFAULT_OPENCODE = Path.home() / ".opencode" / "bin" / "opencode"


def resolve_session_db() -> Path:
    """Locate the active OpenCode database across local and Fly layouts."""
    override = os.getenv("OPENCODE_DB_PATH")
    if override:
        return Path(override)
    persisted = Path("/wf/user_vault/conversations/opencode.db")
    if persisted.exists():
        return persisted
    return Path.home() / ".local" / "share" / "opencode" / "opencode.db"


DEFAULT_SESSION_DB = resolve_session_db()
DEFAULT_AGENT = "wayfinder-job-worker"
_DIAGNOSTIC_TOOL_LIMIT = 25
_DIAGNOSTIC_ERROR_LIMIT = 300
_DIAGNOSTIC_TEXT_LIMIT = 1_500


def _config_source(repo_root: Path) -> Path:
    if (repo_root / ".opencode" / "opencode.json").exists():
        return repo_root
    override = os.environ.get("WAYFINDER_OPENCODE_CONFIG_ROOT")
    candidates = [Path(override).expanduser()] if override else []
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if common.returncode == 0:
        candidates.append(Path(common.stdout.strip()).resolve().parent)
    for candidate in candidates:
        if (candidate / ".opencode" / "opencode.json").exists():
            return candidate
    raise FileNotFoundError(
        "opencode.json is untracked and was not found in the SDK checkout, "
        "its primary worktree, or WAYFINDER_OPENCODE_CONFIG_ROOT"
    )


def build_world_bundle(
    world: Any,
    *,
    sandbox: Path,
    repo_root: Path,
    initial_genome: Genome,
) -> str:
    """Sandbox job bundle: SDK-visible workspace + the world's dev data as
    the job dataset + the interpreter as the strategy. Hidden rows and the
    mechanism NEVER enter the sandbox (audit isolation)."""
    from wayfinder_paths.jobs.models import WayfinderJob
    from wayfinder_paths.jobs.store import JobStore

    install_agent_workspace(sandbox=sandbox, repo_root=repo_root, disable_mcp=True)
    # The file-based WOB worker falls back to the CLI, so MCP stays disabled.
    # Campaign A/B uses the same helper with a lifecycle-only MCP URL.
    store = JobStore(repo_root=sandbox)
    job = WayfinderJob.new(
        f"wob-{world.world_id}",
        goal=(
            "Maximize risk-adjusted return of this strategy by improving its "
            "genome parameters (signal/filter/exit/sizing in "
            "execution_params.genome_spec). The dataset is fixed; there is "
            "no live venue."
        ),
        script="workspace/src/strategy.py",
        agent_mode="intervene",
        execution_contract="jobs_v1",
    )
    store.save(job)
    root = store.job_dir(job.id)
    src_dir = root / "workspace" / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    interpreter = write_interpreter(src_dir)
    interpreter.rename(src_dir / "strategy.py")

    import yaml  # type: ignore[import-untyped]

    job_yaml_path = root / "job.yaml"
    job_yaml = yaml.safe_load(job_yaml_path.read_text()) or {}
    job_yaml["execution_params"] = {
        "genome_spec": initial_genome.to_dict(),
        "fee_bps": world.mechanism.fee_bps,
    }
    job_yaml["script_loop"] = {
        "enabled": True,
        "entrypoint": "workspace/src/strategy.py",
    }
    job_yaml_path.write_text(yaml.safe_dump(job_yaml, sort_keys=False))

    dataset_dir = root / "results" / "backtest"
    dataset_dir.mkdir(parents=True, exist_ok=True)
    rows = [row for path_rows in world.dev_rows for row in path_rows]
    (dataset_dir / "input_bars.json").write_text(
        json.dumps(
            {
                "bars": rows,
                "metadata": {
                    "source": "wob-benchmark",
                    "world_id": world.world_id,
                    "interval": "1h",
                },
            },
            default=str,
        )
    )
    return job.id


def install_agent_workspace(
    *,
    sandbox: Path,
    repo_root: Path,
    disable_mcp: bool = False,
    mcp_url: str | None = None,
    runtime_config: Path | None = None,
) -> None:
    """Make an isolated directory runnable by the real OpenCode agents."""
    sandbox.mkdir(parents=True, exist_ok=True)
    # OpenCode resolves relative tool paths from the discovered project root.
    # Benchmark outputs commonly live below the SDK checkout, so give every
    # arm its own boundary instead of leaking resolution into the parent repo.
    if not (sandbox / ".git").exists():
        initialized = subprocess.run(
            ["git", "init", "--quiet"],
            cwd=sandbox,
            capture_output=True,
            text=True,
            check=False,
        )
        if initialized.returncode != 0:
            raise RuntimeError(
                f"failed to isolate benchmark project root: {initialized.stderr}"
            )
    config_source = _config_source(repo_root)
    target = sandbox / ".opencode"
    if not target.exists():
        workspace_source = (
            repo_root / ".opencode"
            if (repo_root / ".opencode").is_dir()
            else config_source / ".opencode"
        )
        shutil.copytree(
            workspace_source,
            target,
            ignore=shutil.ignore_patterns("node_modules"),
            symlinks=False,
        )
    # The benchmark may pin the exact Shells runtime config. Otherwise the
    # untracked local provider config falls back to the primary checkout;
    # agents and plugins still stay pinned to the declared SDK ref.
    if runtime_config is not None:
        runtime_config = runtime_config.resolve()
        if not runtime_config.is_file():
            raise FileNotFoundError(
                f"OpenCode runtime config is missing: {runtime_config}"
            )
        shutil.copy(runtime_config, target / "opencode.json")
    elif not (target / "opencode.json").exists():
        shutil.copy(config_source / ".opencode" / "opencode.json", target)
    opencode_json = target / "opencode.json"
    if opencode_json.exists():
        config = json.loads(opencode_json.read_text())
        for server in (config.get("mcp") or {}).values():
            if isinstance(server, dict):
                if disable_mcp:
                    server["enabled"] = False
                elif mcp_url:
                    server.update(
                        {
                            "type": "remote",
                            "url": mcp_url,
                            "enabled": True,
                            "timeout": 300_000,
                        }
                    )
        opencode_json.write_text(json.dumps(config, indent=2) + "\n")
    # Code must be INSIDE the sandbox (symlinks trip opencode's
    # external_directory permission wall — found live on pilot wake 0);
    # the venv stays a symlink, agents execute it but rarely read it.
    code_source = repo_root
    if not (code_source / "wayfinder_paths").is_dir():
        raise FileNotFoundError(f"SDK package is missing: {code_source}")
    for name in ("pyproject.toml", "poetry.lock"):
        source = code_source / name
        if source.exists() and not (sandbox / name).exists():
            shutil.copy(source, sandbox / name)
    package = sandbox / "wayfinder_paths"
    if not package.exists():
        shutil.copytree(
            code_source / "wayfinder_paths",
            package,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests"),
        )
    venv_source = (
        repo_root / ".venv"
        if (repo_root / ".venv").exists()
        else config_source / ".venv"
    )
    if venv_source.exists() and not (sandbox / ".venv").exists():
        (sandbox / ".venv").symlink_to(venv_source)


def run_agent_wakes(
    *,
    sandbox: Path,
    job_id: str,
    wakes: int,
    model: str,
    opencode: Path = DEFAULT_OPENCODE,
    agent: str = DEFAULT_AGENT,
    timeout_s: int = 1800,
) -> list[dict[str, Any]]:
    """Drive N intervene wakes through the production harness. Each wake:
    build the REAL worker prompt, hand it to `opencode run --agent`, record
    the session title for token metering."""
    from wayfinder_paths.jobs.store import JobStore
    from wayfinder_paths.jobs.worker import prepare_job_worker_prompt

    store = JobStore(repo_root=sandbox)
    sessions: list[dict[str, Any]] = []
    for wake in range(wakes):
        prepared = prepare_job_worker_prompt(
            store=store, job_id=job_id, mode="intervene"
        )
        session = run_agent_prompt(
            sandbox=sandbox,
            prompt=prepared["prompt"],
            model=model,
            title=f"wob-{job_id}-wake-{wake}",
            opencode=opencode,
            agent=agent,
            timeout_s=timeout_s,
        )
        sessions.append({"wake": wake, **session})
    return sessions


def run_agent_prompt(
    *,
    sandbox: Path,
    prompt: str,
    model: str,
    variant: str | None = None,
    title: str,
    session_id: str | None = None,
    opencode: Path = DEFAULT_OPENCODE,
    agent: str = DEFAULT_AGENT,
    timeout_s: int = 1800,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run one bounded prompt through the real OpenCode harness.

    Campaign benchmarks and synthetic optimizer wakes intentionally share this
    seam: model, agent definition, working directory, timeout behavior, and
    session-title metering must not drift between benchmark lanes.
    """
    command = [
        str(opencode),
        "run",
        "--agent",
        agent,
        "-m",
        model,
        "--dir",
        str(sandbox),
    ]
    if variant:
        command.extend(["--variant", variant])
    if session_id:
        command.extend(["--session", session_id])
    else:
        command.extend(["--title", title])
    command.append(prompt)
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        cwd=sandbox,
        check=False,
        env=env,
    )
    return {
        "title": title,
        "session_id": session_id,
        "exit_code": result.returncode,
        "stdout_tail": result.stdout[-1_500:],
        "stderr_tail": result.stderr[-800:],
    }


def meter_sessions(
    sessions: list[dict[str, Any]],
    *,
    session_db: Path = DEFAULT_SESSION_DB,
) -> dict[str, Any]:
    """Token/call accounting from the opencode session DB (same query seam
    as the eval harness)."""
    totals = {"sessions": 0, "messages": 0, "tokens_in": 0, "tokens_out": 0}
    if not session_db.exists():
        return {**totals, "note": "session db not found"}
    connection = sqlite3.connect(str(session_db))
    try:
        session_ids: list[str] = []
        for session in sessions:
            row = connection.execute(
                "SELECT id FROM session WHERE title=? "
                "ORDER BY time_updated DESC LIMIT 1",
                (session["title"],),
            ).fetchone()
            if not row:
                continue
            session_ids.append(str(row[0]))
        return meter_session_ids(
            session_ids, session_db=session_db, connection=connection
        )
    finally:
        connection.close()


def meter_session_ids(
    session_ids: list[str],
    *,
    since_ms: int | None = None,
    session_db: Path = DEFAULT_SESSION_DB,
    connection: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Meter known OpenCode sessions, optionally from a fixed start time."""
    totals: dict[str, Any] = {
        "sessions": 0,
        "messages": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "tokens_reasoning": 0,
        "tokens_cache_read": 0,
        "tokens_cache_write": 0,
        "tool_calls": 0,
        "tool_result_bytes": 0,
        "tool_result_bytes_by_tool": {},
        "tool_output_bytes": 0,
        "tool_output_bytes_by_tool": {},
        "wall_seconds": 0.0,
        "model_seconds": 0.0,
        "tool_seconds": 0.0,
        "other_seconds": 0.0,
    }
    if not session_ids or (connection is None and not session_db.exists()):
        return totals
    owned = connection is None
    db = connection or sqlite3.connect(str(session_db))
    try:
        for session_id in dict.fromkeys(session_ids):
            try:
                found = db.execute(
                    "SELECT 1 FROM session WHERE id=? LIMIT 1", (session_id,)
                ).fetchone()
            except sqlite3.OperationalError:
                # Small test/legacy databases may predate the session table.
                # In those, message ownership is still enough to prove that
                # this is the persisted session we were asked to meter.
                found = db.execute(
                    "SELECT 1 FROM message WHERE session_id=? LIMIT 1", (session_id,)
                ).fetchone()
            if not found:
                continue
            totals["sessions"] += 1
            starts: list[int] = []
            ends: list[int] = []
            model_intervals: list[tuple[int, int]] = []
            tool_intervals: list[tuple[int, int]] = []
            query = (
                "SELECT json_extract(data,'$.tokens.input'), "
                "json_extract(data,'$.tokens.output'), "
                "json_extract(data,'$.tokens.reasoning'), "
                "json_extract(data,'$.tokens.cache.read'), "
                "json_extract(data,'$.tokens.cache.write'), "
                "json_extract(data,'$.role'), "
                "json_extract(data,'$.time.created'), "
                "json_extract(data,'$.time.completed'), time_created FROM message "
                "WHERE session_id=?"
            )
            params: tuple[Any, ...] = (session_id,)
            if since_ms is not None:
                query += " AND time_created>=?"
                params = (session_id, int(since_ms))
            for (
                tokens_in,
                tokens_out,
                tokens_reasoning,
                tokens_cache_read,
                tokens_cache_write,
                role,
                message_started,
                message_completed,
                row_created,
            ) in db.execute(query, params):
                totals["messages"] += 1
                totals["tokens_in"] += int(tokens_in or 0)
                totals["tokens_out"] += int(tokens_out or 0)
                totals["tokens_reasoning"] += int(tokens_reasoning or 0)
                totals["tokens_cache_read"] += int(tokens_cache_read or 0)
                totals["tokens_cache_write"] += int(tokens_cache_write or 0)
                started = int(message_started or row_created or 0)
                completed = int(message_completed or 0)
                if started:
                    starts.append(max(started, int(since_ms or started)))
                if completed:
                    ends.append(completed)
                    if str(role or "") == "assistant" and completed >= started:
                        model_intervals.append((started, completed))
            part_query = (
                "SELECT json_extract(data,'$.tool'), length(data), "
                "json_extract(data,'$.state.output'), "
                "json_extract(data,'$.state.error'), "
                "json_extract(data,'$.state.time.start'), "
                "json_extract(data,'$.state.time.end'), time_created FROM part "
                "WHERE session_id=? AND json_extract(data,'$.type')='tool'"
            )
            part_params: tuple[Any, ...] = (session_id,)
            if since_ms is not None:
                part_query += " AND time_created>=?"
                part_params = (session_id, int(since_ms))
            by_tool = totals["tool_result_bytes_by_tool"]
            output_by_tool = totals["tool_output_bytes_by_tool"]
            for (
                tool,
                byte_count,
                output,
                error,
                tool_started,
                tool_completed,
                part_created,
            ) in db.execute(part_query, part_params):
                tool_name = str(tool or "unknown")
                size = int(byte_count or 0)
                output_size = sum(
                    len(str(value).encode("utf-8"))
                    for value in (output, error)
                    if value is not None
                )
                totals["tool_calls"] += 1
                totals["tool_result_bytes"] += size
                totals["tool_output_bytes"] += output_size
                by_tool[tool_name] = int(by_tool.get(tool_name) or 0) + size
                output_by_tool[tool_name] = (
                    int(output_by_tool.get(tool_name) or 0) + output_size
                )
                started = int(tool_started or part_created or 0)
                completed = int(tool_completed or 0)
                if started:
                    starts.append(max(started, int(since_ms or started)))
                if completed:
                    ends.append(completed)
                    if completed >= started:
                        tool_intervals.append((started, completed))
            merged_tools = _merge_intervals(tool_intervals)
            session_tool_ms = sum(end - start for start, end in merged_tools)
            session_model_ms = sum(
                end - start
                for start, end in _subtract_intervals(
                    _merge_intervals(model_intervals), merged_tools
                )
            )
            if starts and ends:
                session_wall_ms = max(max(ends) - min(starts), 0)
            else:
                session_wall_ms = session_model_ms + session_tool_ms
            totals["wall_seconds"] += session_wall_ms / 1000
            totals["model_seconds"] += session_model_ms / 1000
            totals["tool_seconds"] += session_tool_ms / 1000
            totals["other_seconds"] += (
                max(session_wall_ms - session_model_ms - session_tool_ms, 0) / 1000
            )
    finally:
        if owned:
            db.close()
    for key in ("wall_seconds", "model_seconds", "tool_seconds", "other_seconds"):
        totals[key] = round(float(totals[key]), 3)
    return totals


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if end < start:
            continue
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _subtract_intervals(
    intervals: list[tuple[int, int]], exclusions: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    remaining: list[tuple[int, int]] = []
    for start, end in intervals:
        cursor = start
        for blocked_start, blocked_end in exclusions:
            if blocked_end <= cursor:
                continue
            if blocked_start >= end:
                break
            if blocked_start > cursor:
                remaining.append((cursor, min(blocked_start, end)))
            cursor = max(cursor, blocked_end)
            if cursor >= end:
                break
        if cursor < end:
            remaining.append((cursor, end))
    return remaining


def session_diagnostic_summary(
    session_id: str,
    *,
    session_db: Path = DEFAULT_SESSION_DB,
    connection: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Return a bounded tool/error trace before an OpenCode session is deleted.

    Full tool inputs and outputs stay out of the archive. The one retained input
    field is a lifecycle ``action`` name, which is needed to distinguish
    evolution_prepare from other calls through the shared core_jobs tool.
    """
    owned = connection is None
    db = connection or sqlite3.connect(str(session_db))
    tool_calls: list[dict[str, str]] = []
    try:
        tool_count = int(
            db.execute(
                "SELECT count(*) FROM part WHERE session_id=? "
                "AND json_extract(data,'$.type')='tool'",
                (session_id,),
            ).fetchone()[0]
        )
        rows = list(
            db.execute(
                "SELECT json_extract(data,'$.tool'), "
                "json_extract(data,'$.state.status'), "
                "json_extract(data,'$.state.input.action'), "
                "json_extract(data,'$.state.error') FROM part "
                "WHERE session_id=? AND json_extract(data,'$.type')='tool' "
                "ORDER BY time_created DESC, id DESC LIMIT ?",
                (session_id, _DIAGNOSTIC_TOOL_LIMIT),
            )
        )
        for tool, status, action, error in reversed(rows):
            entry = {"tool": str(tool or "unknown")}
            if status:
                entry["status"] = str(status)[:80]
            if action:
                entry["action"] = str(action)[:120]
            if error:
                entry["error"] = str(error)[:_DIAGNOSTIC_ERROR_LIMIT]
            tool_calls.append(entry)
        text_row = db.execute(
            "SELECT json_extract(part.data,'$.text') FROM part "
            "JOIN message ON message.id=part.message_id "
            "WHERE part.session_id=? "
            "AND json_extract(part.data,'$.type')='text' "
            "AND json_extract(message.data,'$.role')='assistant' "
            "ORDER BY part.time_created DESC, part.id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    finally:
        if owned:
            db.close()
    final_assistant_text = (
        str(text_row[0])[-_DIAGNOSTIC_TEXT_LIMIT:]
        if text_row and text_row[0] is not None
        else None
    )
    return {
        "schema_version": "1.0",
        "tool_calls": tool_calls,
        "omitted_tool_calls": max(0, tool_count - len(tool_calls)),
        "final_assistant_text": final_assistant_text,
    }


def harvest_lineage(*, sandbox: Path, job_id: str) -> dict[str, Any]:
    """Candidates the agent evaluated/proposed → lineage genomes; the
    currently applied genome → selection. Non-genome proposals are recorded
    but score as no-ops (the grammar lane only credits in-space moves)."""
    import yaml

    from wayfinder_paths.jobs.store import JobStore

    store = JobStore(repo_root=sandbox)
    root = store.job_dir(job_id)
    lineage: list[Genome] = []
    for path in sorted((root / "proposals").glob("*.json")):
        try:
            proposal = json.loads(path.read_text())
        except ValueError:
            continue
        params = (proposal.get("proposed_change") or {}).get("execution_params") or {}
        spec = params.get("genome_spec")
        if isinstance(spec, dict) and spec.get("genome"):
            try:
                lineage.append(Genome.from_dict(spec))
            except (KeyError, TypeError):
                continue
    job_yaml = yaml.safe_load((root / "job.yaml").read_text()) or {}
    active_spec = (job_yaml.get("execution_params") or {}).get("genome_spec")
    selected = None
    if isinstance(active_spec, dict) and active_spec.get("genome"):
        try:
            selected = Genome.from_dict(active_spec)
        except (KeyError, TypeError):
            selected = None
    return {
        "lineage": [(g, 0.0) for g in lineage],
        "selected": selected,
        "optimizer": "agent",
    }
