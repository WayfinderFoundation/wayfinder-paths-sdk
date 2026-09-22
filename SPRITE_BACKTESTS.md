# Run complete backtests on Django-managed Sprites

For configuration-based selection between local execution and Sprites, use the
shared interface documented in [BACKTEST_RUNNERS.md](BACKTEST_RUNNERS.md). The
provider-specific client below remains available for existing integrations.

The remote client packages a job workspace, requests a checkpoint runtime from
`vault-backend` by preset key, uploads the workspace, submits one operation, and
collects its complete artifacts. The Sprite runs this SDK's existing computation
functions using the source commit and Poetry lock baked into its checkpoint.

## Submit a prepared job

Prepare/validate the job's datasets and features using the normal SDK workflow.
Load your existing Shell/user API key as `WAYFINDER_API_KEY`, then run with the SDK
environment from the repository containing `.wayfinder/jobs/<job_id>`:

```bash
python -m wayfinder_paths.jobs.sprite_client \
  --backend https://your-development-backend.example \
  --app-name your-shell-app --preset jobs-v1 \
  --repo /absolute/path/to/workspace --job-id my-job \
  --sdk-commit FULL_COMMIT_SHA \
  --output /absolute/path/to/new-results-directory
```

`--sdk-commit` is optional but recommended for reproducibility; both client and
runtime reject a mismatch. The preset must advertise `sdk-workspace-v1`. This
command leases billed compute and prints its ID, waits for a terminal result,
then extracts artifacts into a new directory. `--submit-only` returns after
submission. Resume retrieval with `--collect LEASE_ID --output NEW_DIRECTORY`
(retain `--backend` and `--app-name`). Owner auth works after worker-token expiry.
The Python client exposes `submit`, `status`, `wait`, `cancel`, and `collect`.
Use it as a context manager to close its HTTP connections. When supplying an
existing `httpx.Client`, the caller retains responsibility for closing that client.

## Computation coverage

| Operation | Inputs/options and behavior |
| --- | --- |
| `backtest_job` (default) | Existing `backtest_execution_job` options, including `quick_bars`, `grid_path`, `parallel`, `workers`, `optimizer`, `optuna_options`, and `walk_forward` |
| `experiments` | Native experiment `grid`, parallelization and other SDK options |
| `robustness_check` | Native robustness plan and options |
| `validate_job`, `preflight` | Existing job validation/preflight functions |
| `pair_check`, `signal_check`, `signal_scan`, `holdout_check`, `rank_check` | Existing research checks over prepared job data |
| `derive_features`, `attribution`, `chart`, `analogs` | Existing derived-feature and analysis functions |
| `script` | Bundled repository-relative Python `path`, optional `argv`; supports core portfolio/multi-leverage backtests and custom compute pipelines |

Pass operation kwargs as a JSON object with `--options options.json` and select
the operation with `--op`. The client supplies `job_id` and `store`; options cannot
replace them. The full SDK result is always saved for backtests and experiments.
For example, a process grid uses:

```json
{"grid_path":"grids/search.json","parallel":"process","workers":2}
```

Add `--extra-path grids/search.json` to ship that file. An Optuna search uses the
SDK's distribution grid schema plus `"optimizer":"optuna"` and
`"optuna_options":{"n_trials":20,"seed":42}`. Walk-forward uses the SDK's
`walk_forward` object (`train_bars`, `test_bars`, `folds`, `warmup_bars`, etc.). The
checkpoint always includes locked `main,ml` dependencies: Optuna, sklearn, joblib,
numpy/pandas, plotting, venue simulators, fees/funding, liquidation and sizing logic.
Custom dependencies must be added to the SDK's lock/build before use.

For `script`, write optional compact output to `result.json` in the working root;
all other files written inside that root are collected too. Use the normal
`if __name__ == "__main__":` guard when starting your own multiprocessing code.
The dedicated compute client/runtime do not require the MCP dependency group.

## What travels and what comes back

The selected `.wayfinder/jobs/<job_id>` tree is copied in full, including source,
configuration, prepared datasets, features, model binaries and prior outputs.
`--extra-path` may be repeated for repository-local helpers/data outside the job.
Keep required files within the source repository. Absolute source-root paths in
JSON/YAML and operation options are rebased inside the isolated runtime. Hardcoded
paths in Python code and files outside the repository need to be made portable.

Machine `config.json`, environment/credential files, `.venv`, Git metadata, caches,
and active operation state do not travel. Symlinks are rejected. No organization
Sprites token or general backend API key goes into the job. The checkpoint has
an empty SDK config; authentication-dependent fetches should run on the Shell
before packaging. Prepared datasets from any supported SDK source travel through
Django's private bucket. Direct worker-side backend exports currently cover
Hyperliquid candles/funding for the separate inline-script contract.

The mailbox returns a compact summary, logs and runtime provenance. The artifact
bundle contains `operation-result.json`, the complete job tree, traces, trades,
folds, models, plots and files from custom scripts. Errors produce
`operation-error.json` and partial artifacts when collection is possible. Undefined
metrics become `null` in the small HTTP summary; full native SDK JSON remains intact.
Uploads use checksum-verified, retryable 8 MiB chunks. Both ends validate the full
archive, and successful completion requires stored artifacts.

Collection never overwrites your original job or automatically applies remote
approval/evidence stamps to it. Artifacts retain the runtime's paths and audit
files; use them for inspection/results and copy changes deliberately. Agent operations use the configured backend when `backtest_runner` is enabled;
without that opt-in they retain their original local behavior. See
[BACKTEST_RUNNERS.md](BACKTEST_RUNNERS.md) for the shared result envelope.

Limits: 512 MiB compressed, 2 GiB expanded and 20,000 files per bundle; default
execution 15 minutes, configurable in the backend preset up to six hours. Large
workloads must fit provider memory/CPU and configured transfer/retention budgets.
Oversized archives fail explicitly. Runtime teardown does not delete stored
artifacts; backend bucket retention controls their availability.

## Build and verification

The `shell/sprites` checkpoint pipeline resolves `wayfinder-jobs-v1` (or another
selected ref) to a full commit, installs its committed source and lock, and runs
`wayfinder_paths/tests/test_sprite_*.py` inside the Sprite before creating
the checkpoint. Commit these modules/tests before building: `git archive` excludes
uncommitted files. Use a new release ID for any SDK, worker or dependency change.

The test matrix executes real portable-workspace computations: single/quick runs,
serial/thread/process grids, Optuna, walk-forward, experiments, robustness,
validation/preflight, legacy portfolio/multi-leverage APIs, ML files and large
artifacts. Transport tests cover retries, checksums, unsafe archives and collection.
Local tests do not establish live provider capacity; run a representative development
Sprite job after configuring the private bucket and registering a new checkpoint.

## Local development

`SpriteWorkspace` owns the portable workspace layout and artifact collection.
`SpriteRoutes` owns the owner and worker API routes. Archive and request metadata
have explicit types while retaining their existing JSON wire format.

Transport tests inject an `httpx.Client` with `MockTransport`; the client's
`sleep` argument makes polling and retry tests immediate. Runtime tests can inject
an `executor` into `run()` without launching a backtest. Execution scopes restore
the working directory, import paths and script arguments after success or failure;
the runtime still requires a dedicated process, not concurrent threads.

Install the optional ML group to exercise the full integration matrix:

```bash
poetry install --with ml
poetry run pytest wayfinder_paths/tests/test_sprite_*.py -o addopts='' -q
```

The suites separate archive rules, HTTP transport, runtime lifecycle, and the
real backtest matrix so each layer can be tested independently.
