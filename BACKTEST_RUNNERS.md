# Configure local or Sprite backtest execution

`BacktestRunner` is the shared interface for `submit`, `submit_archive`, `status`,
`wait`, `cancel`, and `collect`. `create_runner()` chooses the registered provider
named by SDK configuration and environment overrides (`local` and `sprites` ship
today). Callers submit the same job ID, operation, options, and extra workspace
paths, or a registered compute phase, whichever provider runs it.

## Configuration

Add this section to the SDK configuration selected by `WAYFINDER_CONFIG_PATH` /
`WAYFINDER_CONFIG`, or to the workspace's `config.json`:

```json
{
  "backtest_runner": {
    "provider": "local",
    "fallback": "local",
    "runs_dir": ".wayfinder/backtest_runs",
    "retain_runs": 10,
    "extra_paths": [],
    "offload_operations": false,
    "sprites": {
      "backend": "https://your-development-backend.example",
      "app_name": "your-shell-app",
      "preset": "jobs-v1"
    }
  }
}
```

Keep the same configuration across environments and select the provider with:

```bash
export WAYFINDER_BACKTEST_RUNNER=local
# On an environment using remote compute:
export WAYFINDER_BACKTEST_RUNNER=sprites
```

| Environment variable | Configuration field |
| --- | --- |
| `WAYFINDER_BACKTEST_RUNNER` | `backtest_runner.provider` (a registered provider: `local` or `sprites`) |
| `WAYFINDER_BACKTEST_FALLBACK` | `backtest_runner.fallback` (`local`, the default, or `none`) |
| `WAYFINDER_BACKTEST_RUNS_DIR` | `backtest_runner.runs_dir` |
| `WAYFINDER_BACKTEST_TIMEOUT_SECONDS` | `backtest_runner.timeout_seconds` (optional local execution limit) |
| `WAYFINDER_BACKTEST_RETAIN_RUNS` | `backtest_runner.retain_runs` (finished runs and receipts kept; default 10) |
| `WAYFINDER_BACKTEST_SDK_COMMIT` | `backtest_runner.sdk_commit` (optional full Git SHA) |
| `WAYFINDER_SPRITES_BACKEND` | `backtest_runner.sprites.backend` (HTTPS origin; loopback HTTP allowed) |
| `WAYFINDER_SPRITES_APP_NAME` | `backtest_runner.sprites.app_name` |
| `WAYFINDER_SPRITES_PRESET` | `backtest_runner.sprites.preset` |
| `WAYFINDER_API_KEY` | Existing `system.api_key`; required for Sprites only |

`offload_operations` (config only, default `false`) lets standalone agent
operations book a remote lease too (see [Existing agent operations](#existing-agent-operations));
evolution campaigns offload without it.

Environment values take precedence. Relative paths resolve against the job's
repository root. Invalid configuration fails explicitly and never runs locally
instead. Sprite execution time/resource limits are controlled by the selected
Django preset.

### Falling back to local compute

With `fallback: "local"` (the default), a remote provider that refuses to start a
worker runs the computation locally instead: no subscription or credit (HTTP 402
or 403), the active or pool worker limit (409), the daily cap (429), a disabled or
unavailable backend (503), a backend that cannot be reached, a lease that never
becomes ready, or a lease that closed again after being replaced once. Nothing
started remotely in these cases, so nothing runs twice. The run's record carries
`"provider": "local"` and `"fallback": {"from": "sprites", "reason": "..."}`, and
later `status`, `cancel` and `collect` calls reach whichever runner started it.
Authentication, not-found and request errors (400, 401, 404) are configuration
problems and always raise. A failure after a remote run started never retries
locally. Set `fallback: "none"` to raise `ComputeUnavailable` instead. Local runs have no time
limit by default, matching in-place execution; `timeout_seconds` sets one of
1–21,600 seconds, with up to 60 seconds for partial artifact recovery after
interruption.

An optional SDK commit pin requires a matching checkpoint marker or a clean local
SDK checkout at that commit. Modified SDK source cannot satisfy an exact pin.
Without a pin, local execution uses the invoking Python environment and installed
SDK; install the locked `ml` dependency group for ML/Optuna workloads.

## Same command and Python API

```bash
python -m wayfinder_paths.jobs.backtest_cli \
  --repo /absolute/path/to/workspace --job-id my-job \
  --op backtest_job --options /absolute/path/options.json \
  --output /absolute/path/to/new-results-directory
```

Add `--apply` to write the collected results and stamps back into the job, as
described under [Existing agent operations](#existing-agent-operations).
Use `--submit-only` to return a durable run ID immediately. Subsequent invocations
with the same provider/storage/backend configuration can use `--status RUN_ID`,
`--collect RUN_ID --output NEW_DIRECTORY`, or `--cancel RUN_ID`. Submitted runs
survive the client closing or the submitting CLI exiting. `close()` only releases
client resources. Cancellation waits for process cleanup and partial collection.
`wait(run_id, timeout=...)` stops waiting at the deadline, cancels the run and
returns it `timed_out`; without a timeout a local run is waited for as long as it
runs, and a Sprite run until its lease's job timeout plus a transfer window.

```python
from pathlib import Path
from wayfinder_paths.jobs.backtest_runner import create_runner
from wayfinder_paths.jobs.store import JobStore

store = JobStore()
with create_runner(repo_root=store.repo_root) as runner:
    run = runner.submit(
        store, "my-job", op="backtest_job",
        options={"grid_path": "grids/search.json", "parallel": "process", "workers": 2},
        extra_paths=["grids/search.json"],
    )
    result = runner.wait(run["id"])
    if result.get("artifacts"):
        runner.collect(run["id"], Path("new-results-directory"))
```

Both providers return `id`, `provider`, `status`, `result`, `artifacts`, and
`error`. Terminal states are `succeeded`, `failed`, `timed_out`, and `cancelled`.
`result.output` contains the portable runtime summary; `artifacts` contains the
complete archive's `sha256` and `size`. Successful completion requires the full
artifact bundle. Failed or interrupted computations may also have partial artifacts.
Collection verifies the archive and only writes into a fresh directory.

## Compute phases

`run_phase()` offloads one pure function over explicit inputs, without a job:

```python
from pathlib import Path
from wayfinder_paths.jobs.backtest_runner import run_phase
from wayfinder_paths.jobs.compute_phase import compute_phase

@compute_phase
def score_candidates(inputs: Path, outputs: Path, args: dict) -> dict:
    data = (inputs / args["dataset"]).read_bytes()
    (outputs / "scores.parquet").write_bytes(...)
    return {"best": ...}

outcome = run_phase(
    score_candidates, repo_root, ["datasets/eth-1h.parquet"],
    {"dataset": "datasets/eth-1h.parquet"},
)
outcome["result"]        # the function's JSON result, in full
outcome["outputs_path"]  # the collected outputs/ directory
outcome["run"]           # provider, status, logs, and any fallback
```

`pack_inputs(root, paths, request, destination)` archives only the named
repository-relative inputs (credentials, symlinks and `outputs/` are rejected)
with an `evolution_phase` request under the `wayfinder-sprite-phase-v1` protocol.
The runtime verifies every input checksum and the optional SDK pin, then calls
the registered function with the inputs directory, an empty `outputs/` directory
and the JSON arguments. Only `outputs/` comes back, including
`outputs/phase-result.json` or `outputs/phase-error.json`; the inputs, such as a
dataset, never make the return trip. A failed phase raises with its error and
keeps partial outputs in the run receipt.

Inputs that successive phases share go in `run_phase(..., base_paths=[...])`.
They are packed separately by `pack_base` into a reproducible base archive, and
`pack_inputs(..., base=...)` records their checksums as the request's
`base_files` and refuses a path that is in both. The runtime extracts the base
into the same workspace (`--base`) and verifies both archives. A Sprite lease
keeps its base, so a run of phases uploads it once and then only each phase's
own inputs; the local runner copies the base next to the run's archive and
deletes both when the run finishes.

`run_phase()` never applies anything to a job; the caller decides what to do
with the result. Collected outputs live under `runs_dir/receipts/`, which
retention prunes, so copy anything you keep. Phases must be registered with
`@compute_phase` at module level inside `wayfinder_paths`; the runtime refuses
anything else. A remote checkpoint only knows the phases in its SDK commit, so
pin `sdk_commit` when a phase is new. Existing backtest operations keep their
job-workspace behavior unchanged.

### Evolution phases

A campaign's heavy phases run as registered phases whenever a remote provider is
configured: at campaign start the policy scans, failure-mode scan, signal
validation and discovery baseline (`campaign_scans_phase`), the low-fidelity
screen of every candidate (`screen_phase`, including its Optuna tuning preview),
and at finalization full development (`full_dev_phase`, including Optuna tuning)
and the final economic gate (`economic_gate_phase`). Only campaigns book leases:
a lease boots a fresh Sprite and installs the SDK, which pays off over a
campaign's hours of phases but not for a single check. With a remote runner the
MCP `evolution_start` runs as a background operation, since booking can outlast
the synchronous call.
Screening runs for every candidate, so offloading it keeps a Shell's shared CPU
for the agent. Each ships only what it reads: the one candidate bundle, the
campaign manifest, dataset, baseline `source/`, campaign state, the governing
constitution, and the protected certification snapshot when protected folds are
on; screening also ships the campaign's diagnostic pack and the candidate's
reference bundle and cached reference result. The campaign
dataset and the protected snapshot are the phase's base, identical for every
phase of the campaign, so the campaign start books the lease that its
successive phases reuse, and the dataset is uploaded once; everything else is
the per-phase delta.
Other candidates, the job journal and the evidence ledger stay home. The phase
runs the same code over the shipped copy and returns the result plus its
writes: rows it appended to the journal or the evidence-access ledger (with
copy paths mapped back to the repository), a re-tuned candidate `job.yaml`, and a
reference result screening computed. The protected snapshot is never written back,
so it still verifies. Optuna searches are seeded and run one trial at a time, so a
remote search matches the local one trial for trial.

With no runner configured, or `provider: local`, these phases are unchanged: the
supervised in-process child that the heavy lane can pause and whose memory it
bounds. The same child runs when the remote refuses capacity, cannot be
reached, cannot start the phase (for example a checkpoint on another SDK commit),
loses the run, times out or loses its lease before the phase recorded a result,
or when a candidate's entrypoint points outside its bundle; each such case
journals `evolution_phase_ran_locally` with the reason. A successful
remote run journals `evolution_phase_offloaded` with its provider and run id, its
`wall_seconds` and the `node_cpu_seconds` the node itself spent packing, uploading
and polling. Comparing the two shows whether the node stayed responsive; it leaves
no other local resource telemetry. Both rows carry the phase's `purpose`
(`evolution:<phase>`); see [Offload audit trail](#offload-audit-trail). A failure raised by the phase's own code
is classified exactly as the local child does: a contract failure is candidate
evidence, while memory, lock and other infrastructure failures release the claim
for a later retry. Choose a preset whose timeout covers full development
(`WAYFINDER_EVOLUTION_FULL_DEV_TIMEOUT_S`, 5,400 seconds by default), and pin
`sdk_commit` so remote results come from the same code as local ones. The lease
stays booked between phases (the Sprite pauses while idle; the `jobs-v1` profile
allows an hour between phases) and is released when the campaign completes,
journaled as `evolution_leases_released`; a campaign that never completes leaves
it to the lease's idle timeout. The client's `release(lease_id)` (CLI
`--release LEASE_ID`) closes a lease at once.

### Offload audit trail

Every offload decision says why it happened and where the work went, in three places:

- **Job journal.** `evolution_phase_offloaded` has `purpose`, `reason` (the runner
  configuration that sent it away), `destination` and `run_id`.
  `evolution_phase_ran_locally` has `purpose` and the `reason` it stayed on this node.
  `evolution_leases_released` has the campaign and its `lease_ids`.
- **Run receipts** (`runs_dir/receipts/*/run.json`). The submission and the final
  status record `destination`. For Sprites this is `runner`, `provider` (where
  Django's runner profile runs the machine), `lease_id`, `preset`, `worker_host` and
  `backend`. A local run records `{"runner": "local"}`, and a fallback records why
  under `fallback`.
- **Logs** (loguru, stderr of the operation). These lines are logged at `INFO`:
  - "Offloading <phase> for campaign … : <reason>";
  - "Booked <provider> lease <id> (preset, SDK commit) for <purpose>", then "…
    is ready at <worker host>";
  - "Reusing <provider> lease <id> at <host> for <purpose>";
  - "Releasing lease <id>: <reason>", for example `cannot be reused: holds another
    base` or `evolution campaign <id> completed`;
  - "… finished on runner=… provider=… lease_id=…", with wall and node CPU
    seconds.

  These are logged as warnings:
  - a fallback to local compute;
  - a phase that ran on this node instead.

Django's lease audit log (see the backend's `docs/sprite-backtests.md`) records the
same `purpose`. It is on the booking (`lease_booked`) and on each job
(`job_started`), next to the provider and the provider's own machine identifiers.
A purpose is a short label (`^[a-z][a-z0-9_.:-]{0,63}$`) such as
`evolution:screen_phase`, `operation:backtest_job` or `phase:<function>`, never user
data.

## Adding a provider

A provider implements `submit_archive(archive, *, base=None)`, `status`,
`cancel` and `collect`, raises `ComputeUnavailable` when it refuses capacity
before starting anything, and registers itself:

```python
@register_runner("hetzner")
class HetznerRunner(BacktestRunner):
    ...
```

`submit()` for jobs, `run_phase()`, `wait()`, the local fallback, receipts and
the agent entry point then work unchanged. The worker executes the same
`sprite_runtime` entry point on a Python environment with the SDK installed.
Provider settings live in their own `backtest_runner` subsection, parsed in
`load_runner_config`.

## Existing agent operations

Adding `backtest_runner` to SDK config or setting `WAYFINDER_BACKTEST_RUNNER`
enables the abstraction for portable operations launched through the existing
agent/MCP `op_runner` entry point, including detached backtests and experiments.
With a remote provider, a standalone operation books a lease only when
`offload_operations` is `true`. Otherwise it runs in place on this node and logs
why. Booking boots and installs a fresh machine, which is worth it for an
evolution campaign but not for an hourly check.
The normal `core_jobs(action="backtest_job", ...)` call stays the same. `op_status`
returns the shared run envelope, including its provider and collected
`artifacts_path`; the compact summary is under `result.output` in that envelope.
Provider run IDs and recovery records live under `runs_dir/receipts/`.

Configured local and Sprite runs both compute in an isolated copy of the job.
After collection, agent operations apply the run's outputs back to the job
(`applied` in the envelope lists them), so results, validation/preflight stamps,
derived features and ledgers land where in-place execution would have left them
and gates see the new evidence. Each file is compared with its checksum at
packing time:

- A file the run did not change is ignored.
- An output replaces the job's file only if the job has not changed it since
  packing; otherwise the job's newer version is kept and the file is listed under
  `skipped`.
- Append-only `.jsonl` ledgers (journals, trials, features) receive only the
  run's new rows, so rows written concurrently survive.
- The strategy definition — `job.yaml`, `workspace/` and `versions/` — is never
  written back.

Rebased absolute paths and the copy's revision hash are mapped back to the job's,
so stamps name the revision the job was packed at; editing the strategy during a
run therefore still leaves its stamps stale. Failed runs apply their partial
outputs too, as in-place execution would. The evidence-access ledger is recorded
in the source repository when the operation starts.

Without the configuration opt-in, existing agent operations retain their original
in-place local behavior. The new generic CLI/API defaults to the portable local
runner. Live/scheduled execution and authenticated dataset fetching retain their
existing execution paths. Direct calls to SDK computation functions remain the
underlying engine; runner selection happens at the operation boundary, so a
Sprite never recursively submits another Sprite.

## Shared runtime and provider differences

Both providers package the same workspace schema and execute `sprite_runtime`
in a dedicated child process, as does the lower-level `SpriteBacktestsClient`. A
Sprite checkpoint needs a runtime that accepts `--base` and the lease worker.
Supported computations, prepared data/features/models, archive limits and
artifact structure are described in [SPRITE_BACKTESTS.md](SPRITE_BACKTESTS.md).

Local workers run on the current host under `runs_dir/<run_id>/`, with durable
status files, bounded logs, process-group timeout/cancellation, and partial artifact
recovery. An agent operation owns its run: SIGTERM, SIGHUP or Ctrl-C cancels it,
and a local run cancels itself if its owner is killed outright. In a heavy-lane
op child, `op_cancel`'s SIGTERM first cancels the local or remote run, then runs
the lane's own handler, which records the cancellation in the op's status file
and exits with status 143. If a worker itself
dies, the next status check reports the run as failed and kills its computation. Machine config and credential environment variables are not passed to
the compute child. A local process still has the host user's filesystem/network
permissions; it is not a VM security boundary. When a run finishes, its input
bundle and extracted workspace are deleted; the artifact archive stays so
collection can be retried. Starting a run prunes all but the newest `retain_runs`
finished runs, and each agent operation prunes its receipts the same way.

Sprites book a lease through Django, then move bundles directly between this
node and the Sprite with a lease token that only the node and the Sprite hold;
Django sees its hash and keeps only anonymized results. Run ids are
`"<lease id>:<job id>"`, and lease records live in `runs_dir/sprite-leases/`, so
`--status`, `--collect` and `--cancel` work from later invocations on the same
node. Sequential runs reuse the lease; each job starts on a wiped directory and
the Sprite is wiped when the lease idles out, expires or is released. See
[SPRITE_BACKTESTS.md](SPRITE_BACKTESTS.md#leases) for booking, reuse, failure
handling and what Django keeps. A configured and deployed Sprite backend is
required; this SDK abstraction does not provision backend infrastructure. In both
cases, input/output paths and lifecycle calls stay the same for callers.

## Validation

```bash
poetry run pytest wayfinder_paths/tests/test_backtest_runner.py \
  wayfinder_paths/tests/test_sprite_*.py \
  wayfinder_paths/tests/test_evolution_phase_offload.py -o addopts='' -q
```

Runner tests execute real local computations, process grids, client-exit recovery,
timeouts/cancellation, partial artifacts, checksum failures, configuration selection,
the existing agent entry point, heavy-lane cancellation, compute phases, the local
fallback for every refusal status, and a third provider registered only in the test.
The evolution offload tests run screening, full development and the economic gate
in a separate interpreter over exactly the shipped inputs and require the same
result as the local supervised phase, including protected certification and the
Optuna searches (trial for trial; they need the `ml` group).
The Sprite tests use the real HTTP client and portable runtime against an
in-memory implementation of Django's lease endpoints and the Sprite worker API
(`sprite_lease_fake.py`); they do not create billed Sprites or establish live
provider capacity.
