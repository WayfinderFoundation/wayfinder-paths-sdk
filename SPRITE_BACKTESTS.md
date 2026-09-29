# Run complete backtests on leased Sprites

For configuration-based selection between local execution and Sprites, use the
shared interface documented in [BACKTEST_RUNNERS.md](BACKTEST_RUNNERS.md). The
provider-specific client below remains available for direct use.

The client books a Sprite lease from `vault-backend` by preset key, then moves
bundles directly between this node (the Shell or other host running the SDK) and
the Sprite: it uploads a workspace, starts one operation, and downloads its
complete artifacts. The Sprite runs this SDK's existing computation functions
using the source commit and Poetry lock baked into its checkpoint.

## Leases

**Booking.** The node generates a lease token (`secrets.token_urlsafe(32)`) and
sends Django only its SHA-256 with the preset key, authenticated by the owner's
existing `X-API-Key`. Django creates the Sprite worker, makes its URL public
while a worker that enforces the token is serving, and returns the lease: its
id, `worker_url`, expiry, job timeout and idle timeout. A booking whose answer
was lost is repeated with the same token, which Django answers with the same
lease; a lease still `provisioning` is polled every 2 s for up to 10 minutes.

**The lease record.** The token never leaves the node except as the Sprite's
bearer credential. It is stored with the lease id, worker URL, expiry, job
timeout, preset, the base's SHA-256 and any job awaiting collection in
`~/.wayfinder/sprite-leases/<lease id>.json` (the configured runner uses
`runs_dir/sprite-leases/`). The directory is `0700` and each file is created
`0600`. A record is removed once its lease is released or found closed.

**Direct transfer.** Every Sprite request carries `Authorization: Bearer
<lease token>`, which the worker checks against the hash Django gave it:

| Request | Purpose |
| --- | --- |
| `PUT {worker_url}/base` | Once per lease: inputs shared by its jobs, such as a campaign dataset |
| `PUT {worker_url}/workspace` | Once per job: that job's own archive (the delta) |
| `POST {worker_url}/jobs` | Start the job with a node-generated id |
| `GET {worker_url}/jobs/{id}` | Status and the full result, including logs |
| `GET {worker_url}/jobs/{id}/artifacts` | The artifact archive, verified by size and SHA-256 |
| `DELETE {worker_url}/jobs/{id}` | Cancel the job |

Uploads use checksum-verified 8 MiB chunks; the worker verifies each chunk and
the whole archive. Run ids are `"<lease id>:<job id>"`.

**Reuse.** Sequential jobs reuse the node's live lease for the same backend, app
and preset when Django reports it `ready`, its remaining lifetime covers the
job timeout plus two of the lease's transfer windows (`transfer_timeout_seconds`,
reported by Django), and the previous job's results were
collected (or the job was cancelled or left none). The base is uploaded once
per lease: a job with the same base only uploads its delta, and a job needing a
different base releases the lease and books a new one. Otherwise the node
releases the lease and books another; by default an owner holds one open lease.

**Automatic wipe.** When a job starts, the previous job's directory
(workspace, artifacts, logs, result) is wiped, so collect a job's artifacts
before submitting the next; the base stays for the lease. When the lease
reaches its idle timeout (default 10 minutes, at most 1 hour) or its maximum
lifetime (default 1 hour, at most 8 hours), the worker wipes every job, the
base and staged uploads, Django closes the URL and restores or destroys the
Sprite. `--release` (or `release()`) closes and wipes a lease immediately.

**What Django keeps.** Only anonymized data: lease and job lifecycle, sizes,
checksums, and each job's result summary (runtime summary, exit code and
timeout flag, at most 1 MiB) with paths, addresses and credential-looking values
redacted. Bundles, scripts, logs and artifacts never reach Django or any bucket.
No secret crosses between node and Django beyond the owner authentication
header: Django sees only the lease token's hash, and the Sprites organization
token and the worker's own token never reach the node.

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
command books (or reuses) billed compute, prints the run record, waits for a
terminal result, then extracts artifacts into a new directory. `--submit-only`
returns after submission. Resume with `--collect RUN_ID --output NEW_DIRECTORY`
from the node that booked the lease (retain `--backend`, `--app-name` and any
`--lease-dir`). The lease stays open for the next job; `--release LEASE_ID`
closes it now, otherwise it idles out.

The Python client exposes `submit`, `submit_archive`, `status`, `wait`,
`cancel`, `collect` and `release`. `submit_archive(archive,
base=None, require_artifacts=True)` runs an archive you already built
(`pack_job`, or `pack_inputs` over an optional `pack_base` archive); `submit`
packs a job and calls it. Use the client as a context manager to close its HTTP
connections. When supplying an existing `httpx.Client`, the caller retains
responsibility for closing that client.

## Failures and timeouts

- Every call has a timeout: 10 s to connect, 120 s to read during uploads and
  downloads, 30 s otherwise. Connection errors, read timeouts and 502/503/504
  (the proxy may be waking the Sprite) are retried with exponential backoff, five
  attempts in all; other 4xx answers are not.
- A booking refused for capacity (402, 403, 409, 429, 503), an unreachable
  backend (including a gateway still answering 502/504 after the retries), or a
  lease that never becomes ready raises `LeaseUnavailable`, which the runner
  reports as `ComputeUnavailable` so it can run locally. Nothing was started.
  Other refusals (400, 401, 404) are configuration errors and raise.
- If the lease turns out to be closed while a job is being submitted, the node
  drops it, books a new one once, uploads the base again and resubmits; a second
  closed lease raises `LeaseUnavailable`. A closed lease answers `410` when the
  worker idled out itself, and `401`, `403` or a redirect once Django has closed
  its URL or leased the slot again. Any other failure before the job is accepted
  releases the lease and raises.
- A chunk whose answer was lost is sent again. A `409` on an upload re-reads
  `GET /status` and restarts that upload once.
- `wait()` never polls forever. By default it waits for the job timeout plus the
  lease's transfer window and 120 s, never past the lease's expiry plus 120 s.
  Every call it makes, to the worker or to Django, is cut off at that deadline;
  at the deadline it cancels the job (10 s budget) and returns `timed_out` with
  an error saying the node stopped waiting.
- When the worker cannot answer (unreachable, lease closed, or the job wiped),
  Django's anonymized record decides the status. A job that was running when its
  lease closed ends as Django recorded it: `cancelled` if the lease was released,
  otherwise `timed_out`, with the error `Lease closed: <reason>`. A finished job
  whose artifacts were not collected before the lease closed reports `failed`,
  because its artifacts were wiped with the lease.
- Releasing a lease waits for Django's wipe (checkpoint restore included), up to
  four minutes.
- Artifact downloads restart from the beginning up to three times on transport
  errors or a size/checksum mismatch; the destination is written only after the
  archive verifies.
- Cancellation, including SIGTERM, SIGHUP or Ctrl-C in a runner operation,
  cancels the job on the worker and keeps the lease for the next job.
- Raised errors name their lease and job; lease tokens never appear in messages.

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
an empty SDK config; authentication-dependent fetches should run on the node
before packaging. Prepared datasets from any supported SDK source travel inside
the bundle, straight from the node to the Sprite. Direct worker-side backend
exports currently cover Hyperliquid candles/funding for the separate
inline-script contract.

A compute phase (see [BACKTEST_RUNNERS.md](BACKTEST_RUNNERS.md#compute-phases))
ships only the inputs it names and returns only its `outputs/` directory, so a
dataset it reads never travels back. Inputs shared by a lease's phases can be
packed separately with `pack_base`: the runtime's `--base` archive is extracted
into the same workspace, every file of both archives is checksum-verified
(`files` and `base_files` in the request), and a path present in both is an
error. Base archives are reproducible, so packing the same files again yields the
same bytes and the lease's base is reused.

The job's full result carries a compact summary, logs and runtime provenance. The
artifact bundle contains `operation-result.json`, the complete job tree, traces,
trades, folds, models, plots and files from custom scripts. Errors produce
`operation-error.json` and partial artifacts when collection is possible.
Undefined metrics become `null` in the small summary; full native SDK JSON
remains intact. Successful completion requires artifacts unless the job was
submitted with `require_artifacts=False`.

Collection itself never overwrites your original job. Agent operations (and
`backtest_cli --apply`) then apply the run's outputs back to the job with a
three-way check against the packed checksums; see
[BACKTEST_RUNNERS.md](BACKTEST_RUNNERS.md#existing-agent-operations). The runtime
records its workspace root and revision in `sprite-runtime.json` so rebased paths
and stamps map back to the job; checkpoints built before that file existed still
apply outputs, without the mapping. Without the `backtest_runner` opt-in, agent
operations retain their original local behavior.

Limits: 512 MiB compressed, 2 GiB expanded and 20,000 files per bundle (base and
delta each); default execution 15 minutes per job, configurable in the backend
preset, and never past the lease's expiry. Large workloads must fit provider
memory/CPU and the lease lifetime. Oversized archives fail explicitly.

## Build and verification

Every lease boots a fresh Sprite, so it can run any version of this SDK. The node books
with its `sdk_commit`, and a backend `sdk_runtime` runner profile installs that commit during
worker setup: a checksum-pinned CPython 3.12.8, the commit's archive from GitHub, then
`poetry sync --only main,ml` from the committed lock. The commit is the configured pin
(`sdk_commit` / `WAYFINDER_BACKTEST_SDK_COMMIT`), otherwise `node_sdk_commit()`:

| Installed as | Commit |
| --- | --- |
| Shell image (or a Sprite runtime) | `.sdk-commit`, written when the image is built |
| Git checkout | `HEAD` when a remote branch has it; otherwise its nearest main ancestor (`git merge-base HEAD origin/HEAD`, else `origin/main`), with a warning naming it and the newer commits it lacks. Uncommitted SDK changes are warned about too: the Sprite runs the chosen commit without them |
| `pip install git+...` | The commit pip recorded (`direct_url.json`) |
| PyPI release | Its `v<version>` tag, resolved once on GitHub |

When none applies (for example GitHub is unreachable), the booking is refused and the job
runs locally. The node uploads its base bundle during setup; the first job waits for setup,
and the node adds the lease's `setup_timeout_seconds` to its wait. SDK tests run in CI, not
on the Sprite.

The test matrix executes real portable-workspace computations: single/quick runs,
serial/thread/process grids, Optuna, walk-forward, experiments, robustness,
validation/preflight, legacy portfolio/multi-leverage APIs, ML files and large
artifacts. Transport tests cover booking, lease reuse and replacement, retries,
checksums, timeouts, unsafe archives and collection. Local tests do not establish
live provider capacity; run a representative development Sprite job after changing the
pinned commit.

## Local development

`SpriteWorkspace` owns the portable workspace layout and artifact collection.
`SpriteRoutes` owns the owner-authenticated Django lease routes and `LeaseStore`
the node-side lease records. Archive and request metadata have explicit types
while retaining their JSON wire format.

`wayfinder_paths/tests/sprite_lease_fake.py` implements Django's lease endpoints
and the Sprite worker API in memory, running the real runtime for each job, with
switches for lost answers, provisioning, closed leases, conflicts, held jobs and
corrupt downloads. Tests inject it through an `httpx.Client` with
`MockTransport`; the client's `sleep` and `clock` arguments make polling, retries
and deadlines immediate. Runtime tests can inject an `executor` into `run()`
without launching a backtest. Execution scopes restore the working directory,
import paths and script arguments after success or failure; the runtime still
requires a dedicated process, not concurrent threads.

Install the optional ML group to exercise the full integration matrix:

```bash
poetry install --with ml
poetry run pytest wayfinder_paths/tests/test_sprite_*.py -o addopts='' -q
```

The suites separate archive rules, HTTP transport, runtime lifecycle, and the
real backtest matrix so each layer can be tested independently.
