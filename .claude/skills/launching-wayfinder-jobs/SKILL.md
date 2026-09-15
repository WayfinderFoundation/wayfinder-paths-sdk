---
name: launching-wayfinder-jobs
description: The first-release job flow on Shells — pick an off-the-shelf strategy, build one with Strategy Lab, write a freestyle script, or pin an installed Path; read the honest readout; run the launch checklist; launch in paper; customize the long watchdog; go live only through the gate. Load before creating, validating, launching or watching any job.
---

# Launching Wayfinder jobs

One flow for every kind of job. Seven steps, in this order, every time.

| step | what happens | action |
|---|---|---|
| 1 | Pick or build the job | `create_starter` (catalog: `starter_strategies`), Strategy Lab, `create_freestyle`, `create_from_path` |
| 2 | Validate mechanically | `validate_job` — the ladder that fits the kind (static rules + backtest trace for jobs_v1; static rules + a sandboxed three-tick dry run for freestyle; pin + manifest + eval fixtures + dry run for a Path) |
| 3 | Read the evidence back honestly | `readout` (`refresh=true` on a harnessed job runs backtest, walk-forward holdout and robustness in the background) — see `rules/readout.md` |
| 4 | Run the launch checklist | `launch_checklist` — identity (validated revision == deployed revision), mechanical dry run, and every named risk flag — see `rules/launch-checklist.md` |
| 5 | Launch in paper | `launch` (pins the revision, compiles it into the runner, resumes the loops). A weak readout never blocks paper. |
| 6 | Customize the watchdog | `set_watchdog` — watch level, wake cadence, event triggers, notifications, kill switches — see `rules/watchdog.md` |
| 7 | Go live, gated | `acknowledge_risk_flags` for every warn flag, then `launch(script_mode="live", confirm_live=true)`; harnessed jobs also pass the live gate (validation, backtest, preflight at one revision); funding per `developing-jobs-v1-strategies/rules/going-live.md` |

## Kinds and what each gets

- **Harnessed strategies (`jobs_v1`)**: the SDK driver runs `decide()`. Backtest, walk-forward holdout, replication, robustness, preflight, the live gate, research wakes and evolution (every two days, fleet-wide by default). Starters are the off-the-shelf catalog; Strategy Lab builds custom ones.
- **Freestyle scripts (`freestyle_v1`)**: any trigger, any action — "if the Hormuz odds cross X, buy Y perp". A module with `tick(ctx)` that trades only through `ctx.act` (paper fills through the venue's paper broker, live through its real broker). No backtest, no evolution; research wakes read the forward ledger. See `rules/freestyle.md`.
- **Installed Paths (`path_v1`)**: a pinned version of a published Path (`wayfinder path install <slug>` first). The Path runs from its install directory; every tick re-checks the bundle and tree hashes against the pin. No backtest, no evolution. A component without a declared dry-run mode has no paper mode and launches live only after the `no_dry_run` flag is acknowledged. See `rules/paths.md`.

## When the ask does not fit a kind

- Perp, spot-perp or prediction-market rules with any trigger ("if the odds cross X, buy Y perp") → a freestyle job. Venues in v1: `hyperliquid`, `polymarket`, `hyperliquid_prediction`.
- On-chain DeFi actions (swaps, lending, yield rotation) are **not** a freestyle venue in v1: the runtime refuses an `onchain` action and validation reports it. Say exactly that, then offer the fits: an installed Path that does it (`create_from_path`), or a classic strategy job through `core_runner` (`type="strategy"`) with the adapter skills. Never launch a freestyle job whose validation shows a refused venue — but still run `readout`: a failed validation is not the end of the flow, the readout puts the refusal on record (`launch_allowed: false`) and is what you read back when the owner asked for it.
- A described alpha idea on the harnessed universe → Strategy Lab (jobs_v1), so it gets the backtest, holdout and evolution.
- Anything that needs funds moved, gas, or a wallet created is out of the job flow: hand it to the normal execution tools with their safety review.

## Rules that hold for every kind

- Jobs are created **paused**. Nothing ticks before `launch`.
- `validate_job` stamps the workspace revision; `launch_checklist` refuses when the deployed revision differs from the validated one ("validated a, deployed b: re-run validate"). After any edit to `workspace/` (including `risk_limits.json`) the job must be validated and launched again; a launched freestyle/path job refuses to tick on revision drift.
- Risk flags are shown before every paper launch and journaled. A `block` flag (governance ceiling) cannot be acknowledged. Every `warn` flag must be acknowledged, with a memo, before live.
- Never patch a runner env var to change mode or revision; `launch`, `set_script_mode` and `set_watchdog` recompile.
- Never claim a performance number that no artifact carries. The readout's verdict and reasons are the only sentences about performance.
- Report validation as its `status` plus the names of any failed or warned checks, copied from the report. Never a count of checks ("18/18"): counts are the report's to give, and a miscount is a false claim.
- Every readback of a freestyle or Path job that shows a money number (a dry-run fill, equity, a settlement) carries the readout's fixed sentence verbatim: "no backtest exists for this script; nothing here is a performance claim". It is one line; it is never optional.
- Shells wallets are gasless: never check or bridge gas.
- The rules files in this skill carry the whole contract for each kind: building, validating, launching and watching a job needs no reading of the SDK source.
- Job state comes only from `core_jobs` (`list`, `status`). A `not found` means the job does not exist here: say so and stop; never search the filesystem or the SDK source for it, and never build a stand-in unless asked.

## Intervention reviews

A review of a launched job (`review_now`, or the intervene wake) reads the forward ledger — `results/forward/trades.jsonl`, `runs.jsonl`, `summary.json` — and reports it in its own numbers: days, trades, net, streak. A short forward record supports "pause and rework" at most: never "the thesis is falsified", never "the signal is firing now" (validation marks are stub quotes, not the live feed). A recommended change is a proposal with a memo — `code_change` carrying the candidate script, `params_update` for `ctx.params` — so the owner approves it from the proposal list; a halt or pause is recommended with the ledger's numbers and left to the owner (or bounded now with `set_watchdog` kill switches). There is no backtest to cite and no evolution to promise. Blockers and risk flags come verbatim from the checklist and the launch result, not from memory: a missing wallet or risk-limits file is a live-checklist item, not a risk flag. If the wake queue is unavailable, say the review was done directly and stop there.

## Reading the snapshot

`core_jobs(action="status")` carries `readout`, `launch_checklist`, `launch`, `risk_flags`, `watchdog`, `evolution`, `probation_summary`, `research` and `path_upgrade` so the state of the flow is one call away. It also carries `heartbeat` (runner loops, last tick, last wake, launch identity, halt) and `issues` (what is wrong, by code and severity — read this first when asked whether a job is healthy; quote the code and message, never invent a diagnosis), and for freestyle/Path jobs `freestyle` (the script's limits, the last tick's reads and actions, the dry run) and `path` (the pin). See `rules/watchdog.md`.
