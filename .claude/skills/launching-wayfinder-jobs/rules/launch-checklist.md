# The launch checklist

`core_jobs(action="launch_checklist", job_id=…, target="paper"|"live")` returns `items[]`, each `{id, status, detail}` with `status` in `pass | warn | fail | ack_required | info`, plus `identity`, `risk_flags`, `unacknowledged`, `reasons`, `ok`.

## What it proves

- **Identity**: `validation_at_revision` compares the revision stamped on `reports/validation/latest.json` with a fresh hash of `workspace/` + `job.yaml`. Harnessed jobs add `backtest_at_revision` and `preflight_at_revision` (warn for paper, fail for live). Path jobs add `path_pin` (bundle and tree hashes match the pin). This is how "what was backtested is what will run" is enforced: any edit after validation fails the item until validate runs again.
- **Mechanical**: `validation_passed` (all blocking checks) and, for freestyle/path, `mechanical_dry_run` (the sandboxed dry run passed).
- **Risk flags**: one item per flag, `risk:<code>`. `block` → `fail`; `warn` → `warn` for paper, `ack_required` for live until acknowledged; `info` → shown.
- **Live only**: harnessed jobs run `evaluate_live_gate`; freestyle/path need `wallet_label`, a `risk_limits.json` with `max_daily_loss_usd` or `max_drawdown`, and at least `min_paper_runs` (default 20) recorded paper runs.

## How to present it

Read the failing items first, with their `detail` (it names the fix). Then the warn flags as a list: code, message, fix. Then say whether paper is ready. For live, list what still needs acknowledgment and ask for a memo per flag before calling `acknowledge_risk_flags(job_id, risk_flag_codes=[…], memo="…")`.

## Flag catalogue

| code | means | fix |
|---|---|---|
| `no_stop_loss` | nothing bounds a losing position | brackets / `native_stop_required` (harnessed); `max_loss` on actions or `SPEC.max_loss_usd` (freestyle) |
| `no_native_stop` | stops are engine-side only | `execution_params.native_stop_required: true` |
| `no_max_drawdown`, `no_max_daily_loss`, `unbounded_notional`, `no_position_cap`, `no_consecutive_loss_pause` | risk_limits.json gaps | `set_watchdog(kill_switches={…})` |
| `no_kill_switch` | freestyle script has no halt condition and no risk file | `SPEC.halt_when` or kill switches |
| `no_per_tick_notional_cap` | one tick can open unlimited size | `SPEC.max_notional_per_tick` |
| `custom_actions` | `ctx.custom` venue calls cannot be papered | acknowledge; live needs `allow_custom_actions` + `SPEC.custom_risk_acknowledged` |
| `no_dry_run` | Path component declares no dry-run mode | declare `job.dry_run: supported` in wfpath.yaml, or acknowledge and launch live |
| `leverage_above_governance` | block: leverage above the owner ceiling | lower `execution_params.leverage` |
| `no_timeout` | tick can run unbounded | `script_loop.timeout_seconds` in (0, 3600] |

## What `launch` returns

`launch` answers with `launched`, `revision`, the checklist it ran, the flags it showed, and the runner's own results under `compile` and `loops`. Read those two: a launch whose `loops` or `compile` carry an error (`connect_failed`, a missing daemon) has pinned the revision but nothing is ticking — say exactly that, never "running", and check `core_runner_status` before retrying. Report the pinned revision and every flag shown, verbatim from the result, not from memory.
