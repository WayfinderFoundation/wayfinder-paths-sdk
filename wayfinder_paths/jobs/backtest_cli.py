"""Run a prepared job using the configured local or Sprite backend."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from wayfinder_paths.jobs.backtest_runner import create_runner
from wayfinder_paths.jobs.sprite_bundle import OPERATIONS, apply_job_outputs
from wayfinder_paths.jobs.store import JobStore


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--job-id")
    parser.add_argument("--op", choices=sorted(OPERATIONS), default="backtest_job")
    parser.add_argument(
        "--options", type=Path, help="JSON object with native SDK operation options"
    )
    parser.add_argument("--extra-path", action="append", default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="After collection, apply the run's results and stamps to the job",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--submit-only", action="store_true")
    mode.add_argument("--collect", metavar="RUN_ID")
    mode.add_argument("--status", metavar="RUN_ID")
    mode.add_argument("--cancel", metavar="RUN_ID")
    args = parser.parse_args(argv)
    if not any((args.collect, args.status, args.cancel, args.job_id)):
        parser.error("--job-id is required for submission")
    if not any((args.submit_only, args.status, args.cancel, args.output)):
        parser.error("--output is required for collection")
    if args.apply and any((args.submit_only, args.status, args.cancel)):
        parser.error("--apply requires collection")
    options = {}
    if args.options:
        try:
            options = json.loads(args.options.read_text())
            if not isinstance(options, dict):
                raise ValueError("expected a JSON object")
        except (OSError, ValueError) as exc:
            parser.error(f"Cannot read --options: {exc}")
    if args.output and args.output.exists():
        parser.error("--output must be a new directory")
    with create_runner(repo_root=args.repo) as runner:
        if args.status:
            print(json.dumps(runner.status(args.status), indent=2))
            return
        if args.cancel:
            runner.cancel(args.cancel)
            print(json.dumps(runner.wait(args.cancel), indent=2))
            return
        if args.collect:
            run_id = args.collect
        else:
            submitted = runner.submit(
                JobStore(repo_root=args.repo),
                args.job_id,
                op=args.op,
                options=options,
                extra_paths=args.extra_path,
            )
            run_id = submitted["id"]
            print(json.dumps(submitted), flush=True)
            if args.submit_only:
                return
        result = runner.wait(run_id)
        if result.get("artifacts"):
            runner.collect(run_id, args.output)
            if args.apply:
                result["applied"] = apply_job_outputs(
                    JobStore(repo_root=args.repo), args.output
                )
        print(json.dumps(result, indent=2))
        if result["status"] != "succeeded":
            raise SystemExit(1)


if __name__ == "__main__":
    main()
