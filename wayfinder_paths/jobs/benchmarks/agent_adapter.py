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

    sandbox.mkdir(parents=True, exist_ok=True)
    # Agent definitions + MCP config: the sandbox must BE an opencode-able
    # workspace with the production agent available.
    for name in (".opencode", ".mcp.json"):
        source = repo_root / name
        target = sandbox / name
        if source.exists() and not target.exists():
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copy(source, target)

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

    import yaml

    job_yaml_path = root / "job.yaml"
    job_yaml = yaml.safe_load(job_yaml_path.read_text()) or {}
    job_yaml["execution_params"] = {
        "genome_spec": initial_genome.to_dict(),
        "fee_bps": world.mechanism.fee_bps,
    }
    job_yaml["script_loop"] = {"enabled": True, "entrypoint": "workspace/src/strategy.py"}
    job_yaml_path.write_text(yaml.safe_dump(job_yaml, sort_keys=False))

    dataset_dir = root / "results" / "backtest"
    dataset_dir.mkdir(parents=True, exist_ok=True)
    rows = [row for path_rows in world.dev_rows for row in path_rows]
    (dataset_dir / "input_bars.json").write_text(
        json.dumps(rows, default=str)
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
        prepared = prepare_job_worker_prompt(job_id, mode="intervene", store=store)
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
