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
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.benchmarks.compiler import write_interpreter
from wayfinder_paths.jobs.benchmarks.grammar import Genome

DEFAULT_OPENCODE = Path.home() / ".opencode" / "bin" / "opencode"
DEFAULT_SESSION_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
DEFAULT_AGENT = "wayfinder-job-worker"
# opencode.json (provider config) is untracked; worktrees lack it. Fall back
# to the primary checkout when the given repo_root is a bare worktree.
PRIMARY_CHECKOUT = Path("/Users/adrianhaldenby/Documents/wayfinder-paths-sdk")


def _config_source(repo_root: Path) -> Path:
    if (repo_root / ".opencode" / "opencode.json").exists():
        return repo_root
    return PRIMARY_CHECKOUT


def build_world_bundle(
    world: Any,
    *,
    sandbox: Path,
    repo_root: Path,
    initial_genome: Genome,
    agent_mode: str = "auto",
    agent_wake_seconds: int = 300,
) -> str:
    """Sandbox job bundle: SDK-visible workspace + the world's dev data as
    the job dataset + the interpreter as the strategy. Hidden rows and the
    mechanism NEVER enter the sandbox (audit isolation)."""
    from wayfinder_paths.jobs.models import WayfinderJob
    from wayfinder_paths.jobs.store import JobStore

    sandbox.mkdir(parents=True, exist_ok=True)
    # The sandbox must BE an opencode-able SDK workspace: agent definitions
    # + provider config (opencode.json is UNTRACKED — source it from a real
    # checkout, not a bare worktree), the venv + package symlinked so the
    # `wayfinder job` CLI works, and MCP servers disabled (file-based
    # worlds; the worker falls back to the CLI — eval-harness pattern).
    config_source = _config_source(repo_root)
    target = sandbox / ".opencode"
    if not target.exists():
        shutil.copytree(
            config_source / ".opencode", target,
            ignore=shutil.ignore_patterns("node_modules"),
            symlinks=False,
        )
    opencode_json = target / "opencode.json"
    if opencode_json.exists():
        config = json.loads(opencode_json.read_text())
        for server in (config.get("mcp") or {}).values():
            if isinstance(server, dict):
                server["enabled"] = False
        opencode_json.write_text(json.dumps(config, indent=2) + "\n")
    # Code must be INSIDE the sandbox (symlinks trip opencode's
    # external_directory permission wall — found live on pilot wake 0);
    # the venv stays a symlink, agents execute it but rarely read it.
    for name in ("pyproject.toml", "poetry.lock"):
        source = config_source / name
        if source.exists() and not (sandbox / name).exists():
            shutil.copy(source, sandbox / name)
    package = sandbox / "wayfinder_paths"
    if not package.exists():
        shutil.copytree(
            config_source / "wayfinder_paths", package,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests"),
        )
    venv_source = config_source / ".venv"
    if venv_source.exists() and not (sandbox / ".venv").exists():
        (sandbox / ".venv").symlink_to(venv_source)

    store = JobStore(repo_root=sandbox)
    # A NORMAL job with a normal goal — the harness under test must see the
    # same kind of job it sees in production, not benchmark-flavored prompt
    # text. The agent discovers its situation with its own tools.
    job = WayfinderJob.new(
        f"wob-{world.world_id}",
        goal=(
            "Maximize risk-adjusted return of this strategy. Improve it "
            "through the standard evidence loop: backtests, grids, "
            "walk-forward, and gated proposals."
        ),
        script="workspace/src/strategy.py",
        # The script lane needs a schedule to compile; it gets paused
        # immediately in campaigns (no live venue), but registration must
        # succeed for the agent lane to register after it.
        interval_seconds=3600,
        agent_mode=agent_mode,
        agent_wake_seconds=agent_wake_seconds,
        # Auto mode refuses to run with empty limits (production guard).
        # Benchmark worlds have no live venue: the backtest venue with
        # bounded paper limits satisfies the guard without enabling any
        # real execution surface.
        auto_limits={
            "enabled_venues": ["backtest"],
            "allowed_symbols": ["SYN"],
            "max_notional_per_decision": 1000,
            "max_daily_notional": 10000,
            "max_open_positions": 1,
            "max_open_orders": 2,
        },
        execution_contract="jobs_v1",
    )
    store.save(job)
    root = store.job_dir(job.id)
    src_dir = root / "workspace" / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    interpreter = write_interpreter(src_dir)
    interpreter.rename(src_dir / "strategy.py")

    import yaml

    job_yaml_path = root / "job.yaml"
    job_yaml = yaml.safe_load(job_yaml_path.read_text()) or {}
    job_yaml["execution_params"] = {
        "genome_spec": initial_genome.to_dict(),
        "fee_bps": world.mechanism.fee_bps,
    }
    # NEVER touch script_loop: WayfinderJob.new already configured it fully;
    # a hand-replacement dropped interval_seconds and crashed `job compile`
    # before the agent lane could register (found live).
    job_yaml_path.write_text(yaml.safe_dump(job_yaml, sort_keys=False))

    # Benchmark jobs must not burn wakes on production side-quests: a fresh
    # ideation artifact parks the forced-expedition contract (external
    # research tools are absent in sandboxes — found on pilot wake 0).
    ideation_dir = root / "research" / "ideation"
    ideation_dir.mkdir(parents=True, exist_ok=True)
    from wayfinder_paths.jobs.models import utc_now_iso

    (ideation_dir / "latest.json").write_text(
        json.dumps(
            {
                "generated_at": utc_now_iso(),
                "sources_consulted": [
                    {
                        "tool": "benchmark_bundle",
                        "query": "n/a",
                        "takeaway": "Benchmark world: no external research "
                        "surface exists; optimize the genome on the fixed "
                        "dataset instead.",
                    }
                ],
                "hypotheses": [
                    {
                        "title": "Genome search on the fixed dataset",
                        "thesis": "The only improvable surface is "
                        "execution_params.genome_spec.",
                        "bucket": "testable",
                        "next_step": "wayfinder job backtest/grid, then "
                        "propose better genome params",
                    }
                ],
            }
        )
    )

    # The backtest CLI needs an execution spec at the job root — without it
    # the agent cannot evaluate anything (found on the first e2b run).
    from wayfinder_paths.jobs.execution import ExecutionSpec

    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = "1h"
    (root / "execution_spec.json").write_text(json.dumps(spec.to_dict(), indent=2))

    dataset_dir = root / "results" / "backtest"
    dataset_dir.mkdir(parents=True, exist_ok=True)
    # ONE dev path only: the paths share a timeline, so concatenating them
    # creates duplicate timestamps (pandas reindex failures downstream).
    rows = list(world.dev_rows[0])
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
        title = f"wob-{job_id}-wake-{wake}"
        command = [
            str(opencode), "run", "--agent", agent, "-m", model,
            "--dir", str(sandbox), "--title", title, prepared["prompt"],
        ]
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout_s,
            cwd=sandbox, check=False,
        )
        sessions.append(
            {
                "wake": wake,
                "title": title,
                "exit_code": result.returncode,
                "stdout_tail": result.stdout[-800:],
                "stderr_tail": result.stderr[-500:],
            }
        )
    return sessions


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
        for session in sessions:
            row = connection.execute(
                "SELECT id FROM session WHERE title=? "
                "ORDER BY time_updated DESC LIMIT 1",
                (session["title"],),
            ).fetchone()
            if not row:
                continue
            totals["sessions"] += 1
            for tokens_in, tokens_out in connection.execute(
                "SELECT json_extract(data,'$.tokens.input'), "
                "json_extract(data,'$.tokens.output') FROM message "
                "WHERE session_id=?",
                (row[0],),
            ):
                totals["messages"] += 1
                totals["tokens_in"] += int(tokens_in or 0)
                totals["tokens_out"] += int(tokens_out or 0)
    finally:
        connection.close()
    return totals


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
    return {"lineage": [(g, 0.0) for g in lineage], "selected": selected,
            "optimizer": "agent"}
