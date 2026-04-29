# wayfinder-mcp (Rust frontend)

Tiny axum-based HTTP frontend that speaks the MCP `streamable-http` transport.

- Boots in <100 ms (vs the Python `FastMCP` path's ~7 s cold-start)
- Serves `initialize` + `tools/list` from a static manifest baked at build time
  (`/etc/wayfinder-mcp/tools.json` by default — generate via
  `python -m wayfinder_paths.mcp.manifest`)
- Forwards `tools/call` over a Unix socket to a Python worker
  (`wayfinder_paths/mcp/worker.py`)

The worker holds the loaded `FastMCP` instance for the container's lifetime,
so the heavy SDK imports (web3, ccxt, adapters) are paid **once** at worker
startup, off the user-facing critical path.

## Build

```bash
cd wayfinder_mcp_rs
cargo build --release --bin wayfinder-mcp
# binary at target/release/wayfinder-mcp
```

Strip + install however your packaging flow prefers. Typical Docker pattern:

```dockerfile
FROM rust:1.85-slim-bookworm AS rust-builder
WORKDIR /build
COPY wayfinder_mcp_rs ./
RUN cargo build --release --bin wayfinder-mcp \
 && strip target/release/wayfinder-mcp

FROM <runtime-base>
COPY --from=rust-builder /build/target/release/wayfinder-mcp /usr/local/bin/wayfinder-mcp
RUN python -m wayfinder_paths.mcp.manifest > /etc/wayfinder-mcp/tools.json
CMD ["/usr/local/bin/wayfinder-mcp", \
     "--manifest", "/etc/wayfinder-mcp/tools.json", \
     "--worker-script", "/opt/wayfinder-paths-sdk/wayfinder_paths/mcp/worker.py", \
     "--worker-python", "/opt/wayfinder-paths-sdk/.venv/bin/python"]
```

## Run

```bash
# launch the frontend (spawns the Python worker over a Unix socket itself)
wayfinder-mcp \
  --manifest /etc/wayfinder-mcp/tools.json \
  --worker-script /path/to/wayfinder_paths/mcp/worker.py \
  --worker-python /path/to/.venv/bin/python

# or run the Python worker directly for debugging
python -m wayfinder_paths.mcp.worker --socket /tmp/wayfinder-mcp.sock

# print the tool catalog (use at image build time)
python -m wayfinder_paths.mcp.manifest > tools.json
```

Env overrides (all optional):

| Var | Default | Purpose |
|---|---|---|
| `WAYFINDER_MCP_BINARY` | `wayfinder-mcp` on PATH | Override Rust binary location |
| `WAYFINDER_MCP_LISTEN` | `127.0.0.1:8000` | TCP listen address for streamable-http |
| `WAYFINDER_MCP_MANIFEST` | `/etc/wayfinder-mcp/tools.json` | Path to baked tools/list JSON |
| `WAYFINDER_MCP_WORKER_SOCKET` | `/tmp/wayfinder-mcp.sock` | Unix socket between frontend ↔ worker |
| `WAYFINDER_MCP_WORKER_PYTHON` | `sys.executable` | Python interpreter for the worker |

## Wire protocol

Frontend ↔ worker: line-delimited JSON over Unix socket.

```json
// request
{"id": "call-123", "name": "quote_swap", "arguments": {...}}

// response (success)
{"id": "call-123", "result": {...}}

// response (failure)
{"id": "call-123", "error": {"code": -32000, "message": "...", "traceback": "..."}}
```

The worker holds one shared `FastMCP` instance and dispatches requests
concurrently via `asyncio.create_task`.
