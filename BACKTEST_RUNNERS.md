# Configure local or Sprite backtest execution

`BacktestRunner` is the shared interface for `submit`, `status`, `wait`, `cancel`,
and `collect`. `create_runner()` chooses `LocalRunner` or `SpritesRunner` from SDK
configuration and environment overrides. Callers submit the same job ID, operation,
options, and extra workspace paths for either provider.

## Configuration

Add this section to the SDK configuration selected by `WAYFINDER_CONFIG_PATH` /
`WAYFINDER_CONFIG`, or to the workspace's `config.json`:

```json
{
  "backtest_runner": {
    "provider": "local",
    "runs_dir": ".wayfinder/backtest_runs",
    "timeout_seconds": 900,
    "extra_paths": [],
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
| `WAYFINDER_BACKTEST_RUNNER` | `backtest_runner.provider` (`local` or `sprites`) |
| `WAYFINDER_BACKTEST_RUNS_DIR` | `backtest_runner.runs_dir` |
| `WAYFINDER_BACKTEST_TIMEOUT_SECONDS` | `backtest_runner.timeout_seconds` (local execution limit) |
| `WAYFINDER_BACKTEST_SDK_COMMIT` | `backtest_runner.sdk_commit` (optional full Git SHA) |
| `WAYFINDER_SPRITES_BACKEND` | `backtest_runner.sprites.backend` (HTTPS origin; loopback HTTP allowed) |
| `WAYFINDER_SPRITES_APP_NAME` | `backtest_runner.sprites.app_name` |
| `WAYFINDER_SPRITES_PRESET` | `backtest_runner.sprites.preset` |
| `WAYFINDER_API_KEY` | Existing `system.api_key`; required for Sprites only |

Environment values take precedence. Relative paths resolve against the job's
repository root. Invalid configuration fails explicitly; a Sprite failure does
not cause a local retry or duplicate computation. Sprite execution time/resource
limits are controlled by the selected Django preset. The local timeout defaults
to 900 seconds and supports 1–21,600 seconds, with up to 60 seconds for partial
artifact recovery after interruption.

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

Use `--submit-only` to return a durable run ID immediately. Subsequent invocations
with the same provider/storage/backend configuration can use `--status RUN_ID`,
`--collect RUN_ID --output NEW_DIRECTORY`, or `--cancel RUN_ID`. Submitted runs
survive the client closing or the submitting CLI exiting. `close()` only releases
client resources. Cancellation waits for process cleanup and partial collection.

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

## Existing agent operations

Adding `backtest_runner` to SDK config or setting `WAYFINDER_BACKTEST_RUNNER`
enables the abstraction for portable operations launched through the existing
agent/MCP `op_runner` entry point, including detached backtests and experiments.
The normal `core_jobs(action="backtest_job", ...)` call stays the same. `op_status`
returns the shared run envelope, including its provider and collected
`artifacts_path`; the compact summary is under `result.output` in that envelope.
Provider run IDs and recovery records live under `runs_dir/receipts/`.

Configured local and Sprite runs both use an isolated copy of the submitted job.
They preserve the original workspace and return results/evidence in the collected
artifact directory. Copy changes deliberately; remote or isolated gate stamps are
not automatically applied to an active job. Existing tools that inspect the source
job's saved results still inspect that source job, not the collected copy.

Without the configuration opt-in, existing agent operations retain their original
in-place local behavior. The new generic CLI/API defaults to the portable local
runner. Live/scheduled execution and authenticated dataset fetching retain their
existing execution paths. Direct calls to SDK computation functions remain the
underlying engine; runner selection happens at the operation boundary, so a
Sprite never recursively submits another Sprite.

## Shared runtime and provider differences

Both providers package the same workspace schema and execute `sprite_runtime`
in a dedicated child process. This preserves compatibility with existing Sprite
checkpoints and the lower-level `SpriteBacktestsClient`. Supported computations,
prepared data/features/models, archive limits and artifact structure are described
in [SPRITE_BACKTESTS.md](SPRITE_BACKTESTS.md).

Local workers run on the current host under `runs_dir/<run_id>/`, with durable
status files, bounded logs, process-group timeout/cancellation, and partial artifact
recovery. Machine config and credential environment variables are not passed to
the compute child. A local process still has the host user's filesystem/network
permissions; it is not a VM security boundary. Completed workspaces and archives
remain until explicitly removed, so collection can be retried.

Sprites use the existing Django mailbox, scoped tokens, private storage, checkpoint
release, and backend cleanup. A configured and deployed Sprite backend is required;
this SDK abstraction does not provision backend infrastructure. In both cases,
input/output paths and lifecycle calls stay the same for callers.

## Validation

```bash
poetry run pytest wayfinder_paths/tests/test_backtest_runner.py \
  wayfinder_paths/tests/test_sprite_*.py -o addopts='' -q
```

Runner tests execute real local computations, process grids, client-exit recovery,
timeouts/cancellation, partial artifacts, checksum failures, configuration selection,
and the existing agent entry point. The provider parity test uses the real Sprite
HTTP client and portable runtime with an in-memory HTTP transport; it does not
create billed Sprites or establish live provider capacity.
