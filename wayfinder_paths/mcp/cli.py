"""CLI interface for Wayfinder tools.

Auto-generates click subcommands from the FastMCP server's registered tools.

Usage:
  poetry run python -m wayfinder_paths.mcp.cli [TOOL] [OPTIONS]
  poetry run python -m wayfinder_paths.mcp.cli --help
  poetry run python -m wayfinder_paths.mcp.cli wallets --action list
  poetry run python -m wayfinder_paths.mcp.cli discover --kind strategies
"""

import click

from wayfinder_paths.runner.cli import runner_cli


def main():
    # Runner should work even if optional MCP dependencies aren't installed.
    try:
        from wayfinder_paths.mcp.cli_builder import build_cli
        from wayfinder_paths.mcp.server import mcp

        cli = build_cli(mcp)
    except ModuleNotFoundError as exc:
        if str(exc.name) != "mcp":
            raise

        cli = click.Group(
            name="wayfinder",
            help="Wayfinder Paths CLI (runner-only; install MCP deps for tools).",
        )

        @cli.command(name="mcp-help", help="Explain how to enable MCP tool commands.")
        def _mcp_help() -> None:
            click.echo(
                "MCP dependencies are not installed. Install the project's dev dependencies to enable MCP tools."
            )

    cli.add_command(runner_cli)
    cli(standalone_mode=True)


if __name__ == "__main__":
    main()
