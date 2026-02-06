"""CLI interface for Wayfinder tools.

Auto-generates click subcommands from the FastMCP server's registered tools.

Usage:
  poetry run python -m wayfinder_paths.mcp.cli [TOOL] [OPTIONS]
  poetry run python -m wayfinder_paths.mcp.cli --help
  poetry run python -m wayfinder_paths.mcp.cli wallets --action list
  poetry run python -m wayfinder_paths.mcp.cli discover --kind strategies
"""

from wayfinder_paths.mcp.cli_builder import build_cli
from wayfinder_paths.mcp.server import mcp
from wayfinder_paths.runner.cli import runner_cli


def main():
    cli = build_cli(mcp)
    cli.add_command(runner_cli)
    cli(standalone_mode=True)


if __name__ == "__main__":
    main()
