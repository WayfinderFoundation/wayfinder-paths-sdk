"""Entry point for starting/controlling the local runner.

Usage:
  poetry run python -m wayfinder_paths.runnerd start
  poetry run python -m wayfinder_paths.runnerd status
"""

from __future__ import annotations

from wayfinder_paths.runner.cli import runner_cli


def main() -> None:
    runner_cli(standalone_mode=True)


if __name__ == "__main__":
    main()
