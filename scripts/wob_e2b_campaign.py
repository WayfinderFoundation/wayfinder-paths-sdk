"""Free-running harness campaign in an e2b microVM.

THE measurement the WOB agent lane exists for: the production harness — the
runner daemon waking the auto-worker on cadence, sessions queued into a real
`opencode serve`, proposals gated and AUTO-APPLIED — evolving a job against
a benchmark world, unattended, for a budgeted campaign. No hand-fed prompts:
every wake is generated and delivered by the same machinery as production.

Parameterized for future cross-harness/model benchmarking:
    --model wayfinder/deepseek-v4-pro     (any opencode-routable model)
    --wake-seconds 300 --hours 3 --world smooth_optimum --seed 777001

Usage:
    E2B_API_KEY=... python scripts/wob_e2b_campaign.py --hours 3
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from e2b import Sandbox  # noqa: E402
from e2b.sandbox.commands.command_handle import CommandExitException  # noqa: E402

PRIMARY = Path("/Users/adrianhaldenby/Documents/wayfinder-paths-sdk")
TEMPLATE = "wob-agent-4g"
OPENCODE_PORT = 3096  # OpenCodeClient default (localhost:3096)


class _Result:
    def __init__(self, exit_code: int, stdout: str, stderr: str) -> None:
        self.exit_code, self.stdout, self.stderr = exit_code, stdout, stderr


def run(sbx, cmd, timeout=600, env=None, label="", background=False):
    started = time.time()
    if background:
        handle = sbx.commands.run(cmd, envs=env or {}, background=True)
        print(f"[{label}] backgrounded", flush=True)
        return handle
    try:
        result = sbx.commands.run(cmd, timeout=timeout, envs=env or {})
    except CommandExitException as exc:
        result = _Result(exc.exit_code, "".join(exc.stdout or []), "".join(exc.stderr or []))
    print(f"[{label or cmd[:40]}] exit={result.exit_code} ({time.time()-started:.0f}s)", flush=True)
    if result.exit_code != 0:
        print("  STDERR:", (result.stderr or "")[-500:], flush=True)
    return result


def stage_bundle(scratch: Path, *, archetype: str, seed: int, wake_seconds: int) -> str:
    from wayfinder_paths.jobs.benchmarks.agent_adapter import build_world_bundle
    from wayfinder_paths.jobs.benchmarks.grammar import Genome
    from wayfinder_paths.jobs.benchmarks.worlds import generate_world

    stage = scratch / "bundle"
    if stage.exists():
        shutil.rmtree(stage)
    world = generate_world(archetype, seed=seed)
    initial = Genome(
        "new_high_20", "long", "none", "fixed_time",
        (("hold_bars", 8),), "fixed", (),
    )
    job_id = build_world_bundle(
        world, sandbox=stage, repo_root=REPO_ROOT, initial_genome=initial,
        agent_mode="auto", agent_wake_seconds=wake_seconds,
    )
    shutil.rmtree(stage / "wayfinder_paths")
    (stage / ".venv").unlink(missing_ok=True)
    return job_id


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--world", default="smooth_optimum")
    parser.add_argument("--seed", type=int, default=777001)
    parser.add_argument("--model", default="wayfinder/deepseek-v4-pro")
    parser.add_argument("--wake-seconds", type=int, default=240)
    # This e2b plan caps TOTAL sandbox lifetime at ~1h (extensions cannot
    # outrun it — learned when a 2h campaign died at the 60-minute mark).
    # Campaigns must fit inside one VM life; longer runs need cross-VM
    # resume (harvest + rebuild), not yet built.
    parser.add_argument("--hours", type=float, default=0.9)
    parser.add_argument("--poll-seconds", type=int, default=600)
    args = parser.parse_args()

    wayfinder_key = json.load(open(PRIMARY / "config.json"))["system"]["api_key"]
    scratch = Path(tempfile.mkdtemp(prefix="wob-campaign-"))
    job_id = stage_bundle(
        scratch, archetype=args.world, seed=args.seed, wake_seconds=args.wake_seconds
    )
    print("staged job:", job_id, "at", scratch, flush=True)

    subprocess.run(
        f"cd {scratch}/bundle && COPYFILE_DISABLE=1 tar --no-xattrs -czf ../bundle.tgz . 2>/dev/null",
        shell=True, check=True,
    )
    subprocess.run(
        f"cd {REPO_ROOT} && COPYFILE_DISABLE=1 tar --no-xattrs -czf {scratch}/sdk.tgz "
        "--exclude='__pycache__' --exclude='*.pyc' --exclude='tests' "
        "pyproject.toml README.md wayfinder_paths 2>/dev/null",
        shell=True, check=True,
    )
    from importlib.metadata import distributions

    requirements = sorted(
        f"{d.metadata['Name']}=={d.version}"
        for d in distributions()
        if d.metadata["Name"] and not d.metadata["Name"].startswith("wayfinder")
    )
    (scratch / "requirements.txt").write_text("\n".join(requirements))

    # E2B caps creation timeout at 1h; the poll loop slides it forward.
    sbx = Sandbox.create(
        template=TEMPLATE,
        timeout=3500,
        envs={"WAYFINDER_API_KEY": wayfinder_key},
    )
    print("sandbox:", sbx.sandbox_id, flush=True)
    for name in ("bundle.tgz", "sdk.tgz", "requirements.txt"):
        sbx.files.write(f"/home/user/{name}", (scratch / name).read_bytes())

    env = {"WAYFINDER_API_KEY": wayfinder_key, "HOME": "/home/user"}
    run(sbx, "curl -LsSf https://astral.sh/uv/install.sh | sh", label="uv")
    uv = "$HOME/.local/bin/uv"
    run(sbx, f"{uv} python install 3.12 && {uv} venv /home/user/venv -p 3.12", label="venv", timeout=300)
    run(sbx, f"{uv} pip install --python /home/user/venv/bin/python -r /home/user/requirements.txt",
        label="deps", timeout=900)
    run(sbx, f"mkdir -p /home/user/sdk && tar xzf /home/user/sdk.tgz -C /home/user/sdk 2>/dev/null; "
        f"{uv} pip install --python /home/user/venv/bin/python --no-deps /home/user/sdk",
        label="sdk", timeout=600)
    run(sbx, "printf '#!/bin/sh\\nif [ \"$1\" = \"run\" ]; then shift; exec \"$@\"; fi\\n"
        "echo shim >&2; exit 1\\n' | sudo tee /usr/local/bin/poetry >/dev/null && "
        "sudo chmod +x /usr/local/bin/poetry", label="poetry shim")
    run(sbx, "curl -fsSL https://opencode.ai/install -o /tmp/oc.sh && "
    # Latest upstream: its `run` mode executes full multi-round sessions
    # (1.15's run caps at one round; 1.15's serve executes prompt_async but
    # only in the production fork). The delivery shim below uses `run`.
    "bash /tmp/oc.sh", label="opencode latest", timeout=300)
    run(sbx, "mkdir -p /home/user/wob && tar xzf /home/user/bundle.tgz -C /home/user/wob 2>/dev/null; true",
        label="bundle")

    # Compile = register runner lanes + start the daemon (production path).
    compile_result = run(
        sbx, f"cd /home/user/wob && PATH=/home/user/venv/bin:$PATH "
        f"wayfinder job compile {job_id} > /home/user/compile.log 2>&1; "
        "status=$?; tail -4 /home/user/compile.log; exit $status",
        label="job compile", env=env, timeout=300)
    if compile_result.exit_code != 0:
        print("COMPILE FAILED — aborting campaign", flush=True)
        log = run(sbx, "tail -40 /home/user/compile.log", label="compile log", env=env)
        print((log.stdout or "")[-1800:], flush=True)
        sbx.kill()
        return 1
    # DELIVERY SHIM: the sandbox runs upstream opencode, whose server does
    # not process queued prompt_async (production runs the forked build that
    # does). Swap the agent lane's DELIVERY from queue-to-server to
    # `opencode run` — identical prompt bytes, agent, tools, model, and
    # cadence; only the transport differs. Fork-binary-in-template is the
    # planned fidelity upgrade.
    wrapper = f"""
import subprocess, sys, json, os
from pathlib import Path
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.worker import prepare_job_worker_prompt

store = JobStore(repo_root=Path("/home/user/wob"))
prepared = prepare_job_worker_prompt(store=store, job_id="{job_id}", mode="auto")
result = subprocess.run(
    [os.path.expanduser("~/.opencode/bin/opencode"), "run",
     "--agent", "wayfinder-job-auto-worker", "-m", "{args.model}",
     "--dir", "/home/user/wob", prepared["prompt"]],
    capture_output=True, text=True, timeout=840, cwd="/home/user/wob",
)
store.append_journal("{job_id}", {{
    "type": "benchmark_wake_executed",
    "exit_code": result.returncode,
    "stdout_tail": (result.stdout or "")[-400:],
}})
print(json.dumps({{"exit": result.returncode}}))
"""
    sbx.files.write("/home/user/wob/.wayfinder_runs/jobs/wob_agent_wrapper.py", wrapper)
    run(sbx,
        f"cd /home/user/wob && ls .wayfinder_runs/jobs/ | head -4 && "
        f"AGENT_WRAPPER=$(ls .wayfinder_runs/jobs/*_agent.py | head -1) && "
        f"cp /home/user/wob/.wayfinder_runs/jobs/wob_agent_wrapper.py $AGENT_WRAPPER && "
        f"echo wrapper swapped: $AGENT_WRAPPER",
        label="wrapper swap", env=env)

    # The script lane would tick a live driver against a nonexistent venue —
    # pause it; the improve loop (agent lane) is the campaign's subject.
    run(sbx, f"cd /home/user/wob && PATH=/home/user/venv/bin:$PATH "
        f"wayfinder runner pause {job_id}-script 2>&1 | tail -2 || true",
        label="pause script lane", env=env)
    run(sbx, f"cd /home/user/wob && PATH=/home/user/venv/bin:$PATH "
        "wayfinder runner status 2>/dev/null | grep -E '\"name\"|\"status\"' | head -8",
        label="runner status", env=env)

    deadline = time.time() + args.hours * 3600
    while time.time() < deadline:
        time.sleep(min(args.poll_seconds, max(30, deadline - time.time())))
        try:
            sbx.set_timeout(3500)
        except Exception as exc:  # noqa: BLE001
            print("timeout extension failed:", exc, flush=True)
        poll = run(sbx,
            f"J=/home/user/wob/.wayfinder/jobs/{job_id}; "
            "echo wakes=$(grep -c '\"mode\"' $J/journal.jsonl 2>/dev/null); "
            "echo proposals=$(ls $J/proposals/ 2>/dev/null | wc -l); "
            "echo applied=$(grep -c proposal_promoted $J/journal.jsonl 2>/dev/null); "
            "tail -1 $J/journal.jsonl 2>/dev/null | cut -c1-160",
            label="poll", env=env, timeout=120)
        print(poll.stdout.strip()[-400:], flush=True)

    harvest = run(sbx,
        f"J=/home/user/wob/.wayfinder/jobs/{job_id}; "
        "tar czf /home/user/harvest.tgz -C $J proposals journal.jsonl job.yaml "
        "state ledgers reports 2>/dev/null; ls -la /home/user/harvest.tgz",
        label="harvest", env=env, timeout=300)
    data = sbx.files.read("/home/user/harvest.tgz", format="bytes")
    out = scratch / "harvest.tgz"
    out.write_bytes(data)
    print("harvest saved:", out, len(data), "bytes", flush=True)
    sbx.kill()
    print("CAMPAIGN-DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
