# Local Runs (not committed)

This directory is for **one-off scripts** created during interactive sessions (e.g. Claude Code "execution mode").

- Everything under `.wayfinder_runs/` is gitignored except this README.
- Prefer running scripts via the MCP tool `mcp__wayfinder__run_script` so Claude Code can show a review prompt.
- During a session, put ad-hoc scripts under `.wayfinder_runs/.scratch/<session_id>/` (see `$WAYFINDER_SCRATCH_DIR`). Scratch is auto-deleted on session end.
- Promote **keepers** into `.wayfinder_runs/library/` (see `$WAYFINDER_LIBRARY_DIR`) via `/promote-wayfinder-script` or `poetry run python scripts/promote_wayfinder_script.py`.
  - A “keeper” is a script that worked as intended and you expect to reuse in later sessions (so you don’t have to regenerate/review it from scratch).
  - Promoted scripts are organized under protocol folders (e.g. `.wayfinder_runs/library/hyperliquid/`, `.wayfinder_runs/library/moonwell/`), falling back to `.wayfinder_runs/library/misc/`.
- Prefer using RPCs from `config.json` (`strategy.rpc_urls`) via `wayfinder_paths.core.utils.web3.web3_from_chain_id(...)` instead of hardcoding RPC URLs in scripts.
- Recent on-chain actions (including `deploy_contract`) are recorded in `.wayfinder_runs/wallet_profiles.json` and exposed via the MCP resource `wayfinder://wallets/{label}`.
- Override the directory via `WAYFINDER_RUNS_DIR` (defaults to `.wayfinder_runs`).
