"""WOB agent-lane wake inside an E2B microVM: real OS isolation (the fix for
the bash-escape finding). Bootstraps python 3.12 + SDK + opencode, runs one
production-agent wake against the world bundle, harvests results."""

import json
import os
import sys
import time
from pathlib import Path

from e2b import Sandbox

SP = Path(__file__).parent
WAYFINDER_KEY = json.load(
    open("/Users/adrianhaldenby/Documents/wayfinder-paths-sdk/config.json")
)["system"]["api_key"]


class _Result:
    def __init__(self, exit_code, stdout, stderr):
        self.exit_code, self.stdout, self.stderr = exit_code, stdout, stderr


def run(sbx, cmd, timeout=600, env=None, label=""):
    from e2b.sandbox.commands.command_handle import CommandExitException

    started = time.time()
    try:
        result = sbx.commands.run(cmd, timeout=timeout, envs=env or {})
    except CommandExitException as exc:
        # Nonzero exit raises in the e2b SDK; normalize so failures are
        # reported, not fatal.
        result = _Result(exc.exit_code, "".join(exc.stdout or []), "".join(exc.stderr or []))
    print(
        f"[{label or cmd[:40]}] exit={result.exit_code} "
        f"({time.time() - started:.0f}s)",
        flush=True,
    )
    if result.exit_code != 0:
        print("  STDERR:", (result.stderr or "")[-600:], flush=True)
        print("  STDOUT:", (result.stdout or "")[-300:], flush=True)
    return result


sbx = Sandbox.create(template="wob-agent-4g", timeout=3600, envs={"WAYFINDER_API_KEY": WAYFINDER_KEY})
print("sandbox:", sbx.sandbox_id, flush=True)

for name in ("e2b_bundle.tgz", "e2b_sdk.tgz", "e2b_requirements.txt", "e2b_prompt.txt"):
    sbx.files.write(f"/home/user/{name}", (SP / name).read_bytes())
    print("uploaded", name, flush=True)

run(sbx, "curl -LsSf https://astral.sh/uv/install.sh | sh", label="install uv")
UV = "$HOME/.local/bin/uv"
run(sbx, f"{UV} python install 3.12 && {UV} venv /home/user/venv -p 3.12",
    label="python 3.12 venv", timeout=300)
run(sbx,
    f"{UV} pip install --python /home/user/venv/bin/python "
    "-r /home/user/e2b_requirements.txt",
    label="deps", timeout=900)
run(sbx,
    "mkdir -p /home/user/sdk && tar xzf /home/user/e2b_sdk.tgz -C /home/user/sdk && "
    f"{UV} pip install --python /home/user/venv/bin/python --no-deps /home/user/sdk",
    label="sdk install", timeout=600)
# `poetry run X` shim -> exec X from the venv PATH (no poetry in the VM).
run(sbx,
    "printf '#!/bin/sh\\nif [ \"$1\" = \"run\" ]; then shift; exec \"$@\"; fi\\n"
    "echo \"poetry shim: only run supported\" >&2; exit 1\\n' "
    "| sudo tee /usr/local/bin/poetry >/dev/null && sudo chmod +x /usr/local/bin/poetry",
    label="poetry shim")
run(sbx, "curl -fsSL https://opencode.ai/install | bash", label="install opencode",
    timeout=300)
run(sbx, "mkdir -p /home/user/wob && tar xzf /home/user/e2b_bundle.tgz -C /home/user/wob",
    label="bundle extract")
check = run(sbx,
    "PATH=/home/user/venv/bin:$PATH wayfinder job gate wob-smooth_optimum-777001 "
    "2>&1 | tail -3 || true",
    label="cli smoke", env={"HOME": "/home/user"},
    timeout=300)
print("CLI:", (check.stdout or check.stderr or "")[-300:], flush=True)

run(sbx, "ls -la $HOME/.opencode/bin/ 2>&1; $HOME/.opencode/bin/opencode --version 2>&1",
    label="opencode check", env={"HOME": "/home/user"})
wake = run(sbx,
    'cd /home/user/wob && PATH=/home/user/venv/bin:$PATH '
    '$HOME/.opencode/bin/opencode run --agent wayfinder-job-worker '
    '-m wayfinder/deepseek-v4-pro --dir /home/user/wob '
    '--title wob-e2b-wake-0 "$(cat /home/user/e2b_prompt.txt)" 2>&1',
    label="WAKE", timeout=1500,
    env={"WAYFINDER_API_KEY": WAYFINDER_KEY, "HOME": "/home/user"})
print("WAKE STDOUT tail:", (wake.stdout or "")[-800:], flush=True)

harvest = run(sbx,
    "J=/home/user/wob/.wayfinder/jobs/wob-smooth_optimum-777001; "
    "echo '--- proposals'; ls $J/proposals/ 2>/dev/null; "
    "echo '--- journal'; tail -5 $J/journal.jsonl 2>/dev/null | cut -c1-200; "
    "echo '--- reports'; find $J/reports -name latest.json 2>/dev/null "
    "-exec sh -c 'head -c 400 {}' \\;",
    label="harvest", timeout=120)
print(harvest.stdout[-1500:], flush=True)
sbx.kill()
print("E2B-RUN-DONE", flush=True)
