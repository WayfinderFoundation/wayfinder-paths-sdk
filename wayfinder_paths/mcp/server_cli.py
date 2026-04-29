"""`wayfinder mcp` subcommand group.

Three subcommands:

  wayfinder mcp serve      — spawn the Rust frontend (which spawns the worker
                             internally over a Unix socket). Default for opencode
                             integration.
  wayfinder mcp worker     — run the Python worker daemon directly (invoked by
                             the Rust frontend; manual invocation only useful for
                             debugging).
  wayfinder mcp manifest   — print the tool manifest JSON to stdout. Run at image
                             build time to bake `/etc/wayfinder-mcp/tools.json`.

The Rust binary is built from `wayfinder_mcp_rs/` and is expected on PATH as
`wayfinder-mcp` (typical install path: `cargo build --release -p wayfinder-mcp`
then copy `target/release/wayfinder-mcp` into the runtime image). The CLI does
not build the binary itself — that's the consumer's responsibility (Dockerfile
or maturin wrapper).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

import click

from wayfinder_paths.mcp import manifest as manifest_mod
from wayfinder_paths.mcp import worker as worker_mod


def _default_worker_socket() -> str:
    return os.environ.get("WAYFINDER_MCP_WORKER_SOCKET", "/tmp/wayfinder-mcp.sock")


def _default_manifest_path() -> str:
    return os.environ.get("WAYFINDER_MCP_MANIFEST", "/etc/wayfinder-mcp/tools.json")


def _default_listen() -> str:
    return os.environ.get("WAYFINDER_MCP_LISTEN", "127.0.0.1:8000")


def _default_python() -> str:
    return os.environ.get("WAYFINDER_MCP_WORKER_PYTHON", sys.executable)


def _resolve_rust_binary() -> str:
    explicit = os.environ.get("WAYFINDER_MCP_BINARY")
    if explicit:
        return explicit
    found = shutil.which("wayfinder-mcp")
    if found:
        return found
    raise click.ClickException(
        "wayfinder-mcp binary not found on PATH. Build with `cargo build --release -p wayfinder-mcp` "
        "from wayfinder_mcp_rs/ in the SDK source tree, then install the resulting "
        "target/release/wayfinder-mcp. Or set WAYFINDER_MCP_BINARY to its location."
    )


@click.group(name="mcp", help="Wayfinder MCP server (Rust frontend + Python worker).")
def mcp_cli() -> None:
    pass


@mcp_cli.command("serve")
@click.option("--listen", default=None, help="ip:port the Rust frontend binds. Default 127.0.0.1:8000.")
@click.option("--manifest", default=None, help="Path to baked tools/list manifest JSON.")
@click.option("--worker-socket", default=None, help="Unix socket path the worker listens on.")
@click.option("--worker-python", default=None, help="Python interpreter for the worker subprocess.")
def mcp_serve(
    listen: str | None,
    manifest: str | None,
    worker_socket: str | None,
    worker_python: str | None,
) -> None:
    """Spawn the Rust MCP frontend (it spawns the Python worker itself)."""
    binary = _resolve_rust_binary()
    listen = listen or _default_listen()
    manifest = manifest or _default_manifest_path()
    worker_socket = worker_socket or _default_worker_socket()
    worker_python = worker_python or _default_python()

    if not Path(manifest).exists():
        raise click.ClickException(
            f"Manifest not found at {manifest}. "
            "Run `wayfinder mcp manifest > {manifest}` at build time to bake it."
        )

    args = [
        binary,
        "--listen", listen,
        "--manifest", manifest,
        "--worker-socket", worker_socket,
        "--worker-python", worker_python,
        "--worker-script", str(Path(worker_mod.__file__).resolve()),
    ]
    os.execvp(binary, args)


@mcp_cli.command("worker")
@click.option("--socket", "socket_path", required=True, help="Unix socket path to listen on.")
def mcp_worker(socket_path: str) -> None:
    """Run the Python worker daemon directly (manual debug; normally spawned by the Rust frontend)."""
    asyncio.run(worker_mod.run(socket_path))


@mcp_cli.command("manifest")
def mcp_manifest() -> None:
    """Print the tool manifest JSON to stdout (run at image build time).

    Spawns a fresh Python subprocess that imports
    wayfinder_paths.mcp.manifest standalone — the manifest module sets
    OPENCODE_INSTANCE_ID before importing the FastMCP server so the
    `read_resource` tool registers (it's gated on that env var). Importing
    manifest.main() in-process here would be too late, since the parent
    `wayfinder` CLI has already loaded wayfinder_paths.mcp.server with
    the env unset, and the gate has already been evaluated.
    """
    os.execvp(sys.executable, [sys.executable, "-m", "wayfinder_paths.mcp.manifest"])


if __name__ == "__main__":
    mcp_cli(standalone_mode=True)
