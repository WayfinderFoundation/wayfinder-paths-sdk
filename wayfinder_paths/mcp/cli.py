"""CLI interface for Wayfinder tools.

Auto-generates click subcommands from the FastMCP server's registered tools.

Usage:
  poetry run python -m wayfinder_paths.mcp.cli [TOOL] [OPTIONS]
  poetry run python -m wayfinder_paths.mcp.cli --help
  poetry run python -m wayfinder_paths.mcp.cli wallets --action list
  poetry run python -m wayfinder_paths.mcp.cli discover --kind strategies
"""

import sys

import click

from wayfinder_paths.core.api_origin import UntrustedApiOriginError
from wayfinder_paths.mcp.cli_builder import build_cli
from wayfinder_paths.mcp.server import mcp
from wayfinder_paths.paths.cli import path_cli
from wayfinder_paths.paths.heartbeat import maybe_heartbeat_installed_paths
from wayfinder_paths.runner.cli import runner_cli


def _first_command(argv: list[str]) -> str | None:
    for arg in argv:
        if not arg or arg.startswith("-"):
            continue
        return arg
    return None


def _run() -> None:
    first_command = _first_command(sys.argv[1:])
    if first_command and first_command not in {"path", "runner"}:
        maybe_heartbeat_installed_paths(trigger="mcp-cli")

    cli = build_cli(mcp)
    cli.add_command(runner_cli)
    cli.add_command(path_cli)
    cli(standalone_mode=True)


def main():
    # A refused API URL is a configuration error, not a crash: print what is
    # wrong and how to fix it instead of a traceback.
    try:
        _run()
    except UntrustedApiOriginError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(2)


if __name__ == "__main__":
    main()
