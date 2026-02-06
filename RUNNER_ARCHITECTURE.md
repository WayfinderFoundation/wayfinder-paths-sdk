# Runner Architecture (extensible)

This repo includes a **project-local runner daemon** that schedules jobs (strategies + scripts) on an interval.

The current implementation is **local-only** (Unix domain socket control plane + subprocess workers), but the
code is intentionally split so we can add a **remote/cloud runner** later without rewriting the scheduler.

## Components

### `runnerd` (daemon)

- Code: `wayfinder_paths/runner/daemon.py`
- Owns:
  - SQLite state (`state.db`) and job/run history
  - Interval scheduling loop (tick-based)
  - Spawning worker subprocesses and collecting exit status
  - Per-run log files

### Control plane (transport-agnostic routing)

- Request/response protocol: `wayfinder_paths/runner/protocol.py`
  - JSON-line RPC framing and encode/decode helpers.
  - This stays stable as we add new transports.
- Method routing: `wayfinder_paths/runner/api.py`
  - Maps `method` → `daemon.ctl_*` calls.
  - Transport-independent so it can be reused by a future HTTP server.

### Control transport (pluggable)

- Transport interface + Unix socket transport: `wayfinder_paths/runner/transport.py`
  - Today: `UnixSocketTransport` → `./.wayfinder/runner/runner.sock`
  - Future: add an `HttpTransport` and an HTTP server that calls the same `api.dispatch()`.
- Client: `wayfinder_paths/runner/client.py`
  - Uses the protocol helpers + transport to issue control calls.

## State directory (container-friendly)

By default runner state lives in the repo:

- `./.wayfinder/runner/state.db`
- `./.wayfinder/runner/logs/`
- `./.wayfinder/runner/runner.sock`

For Docker/VM deployments you can move the runner state (DB + logs + socket) with:

- `WAYFINDER_RUNNER_DIR=/path/to/state` (or `WAYFINDER_RUNNER_STATE_DIR`)

If the path is relative, it’s resolved relative to the repo root.

## Future: remote/cloud runner (planned)

When we add a cloud/remote deployment, we can do it with minimal changes by extending the seams above:

1. Add a **new control transport** (HTTP) alongside Unix sockets.
2. Add an HTTP server that:
   - uses the same JSON request/response shapes (or a thin mapping layer)
   - calls `wayfinder_paths.runner.api.dispatch()` for behavior
3. Add “profiles/endpoints” to CLI/MCP so `runner status/add-job/...` can target either:
   - local runner (Unix socket)
   - remote runner (HTTP + token), running in a VM/container with a persistent volume for `WAYFINDER_RUNNER_DIR`

We’re not enabling the remote transport yet; this document just describes the intended extension path.

