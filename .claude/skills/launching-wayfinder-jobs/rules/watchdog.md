# The long watchdog

`core_jobs(action="set_watchdog", job_id=…, watch_level=…, agent_wake_seconds=…, cron_expr=…, timezone=…, triggers=[…], trigger_debounce_seconds=…, notifications={…}, kill_switches={…})`. `launch` accepts the same dict as `watchdog={…}` so the settings ride the launch compile. `status` carries the current view under `watchdog`.

## The four dials

- **Watch level** (`off | monitor | intervene | auto`). `monitor` reads and reports; `intervene` also researches and files proposals the owner approves; `auto` acts inside `auto_limits`. Defaults: harnessed jobs `intervene`; freestyle/path `monitor`. Say plainly that `monitor`/`off` make a harnessed job evolution-ineligible (the response's `warnings` says so too).
- **Cadence**: `agent_wake_seconds` (interval, default 3600; 900 for auto) or `cron_expr` + `timezone`. Interval and cron are exclusive.
- **Triggers**: events that wake the agent immediately: `script_failure`, `drift_warning`, `health_red`, `proposal_created`, `reconcile_mismatch`, `risk_halt`, `regime_shift`. Infrastructure events (`runner_loop_gap`, `disk_pressure`, `verdict_matured`, …) always wake. `trigger_debounce_seconds` spaces event wakes (default 600).
- **Notifications**: `{"channels": ["chat","email","sms"], "on": [events…], "quiet_hours": {"start": "22:00", "end": "07:00", "tz": "…"}}`. Chat is the wake report; email/sms go through the notify client, one per event per hour at most. Defaults: harnessed `health_red, risk_halt, proposal_created`; freestyle/path `script_failure, risk_halt, runner_loop_gap`.

## Kill switches

`kill_switches={"max_drawdown": 0.10, "max_daily_loss_usd": 25, "pause_after_consecutive_losses": 5, "max_gross_exposure_usd": …, "max_position_per_symbol_usd": …}` writes `workspace/risk_limits.json` (drawdown is stored negative). That is a workspace change, so the revision moves: a harnessed job kicks the gate restamp; a launched freestyle/path job is re-validated and re-launched in its current mode, and a failed re-validation restores the previous limits so the running loop is never orphaned. Tell the user the revision moved.

## Research alongside

The intervene wake carries the research lane: it reads the forward ledger, runs the ideation cadence (due 20 h, overdue 48 h) and files proposals with a memo; the owner approves. `status.research.ideation` shows the latest ideation artifact. For freestyle and path jobs the worker is told there is no backtest and no evolution; its recommendations are `memo` or `parameter` proposals with a script diff.

## Evolution every two days

Harnessed jobs at `intervene`/`auto` with a canonical dataset are evolution-eligible; a campaign runs about every 48 hours, one per box at a time, oldest-first when many are due. `status.evolution` shows eligibility, the campaign status and `next_due_at`; `status.probation_summary` lists every probation trial (paired days, candidate vs reference PnL, the paired estimate and its bounds). Probation can kill a candidate; it cannot prove one — say so when reading a trial back.
