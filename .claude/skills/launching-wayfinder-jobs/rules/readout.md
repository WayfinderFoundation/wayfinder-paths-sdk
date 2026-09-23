# The honest readout

`core_jobs(action="readout", job_id=…)` writes `reports/readout/latest.json` and returns it. Read it back to the user in this shape, in this order:

1. **Money first, in plain words.** For a harnessed job: net return after costs on the backtest window, the chronologically last walk-forward fold (the small holdout) on its own, how many out-of-sample folds were positive, how much of the in-sample return survived (`decay_ratio`), whether replication on the refreshed dataset still holds, and how much of gross profit fees take. Quote the numbers from `evidence`; do not round them into adjectives. `evidence.benchmark` carries buy-and-hold over the same window (and the holdout fold) beside the strategy's return with the difference — read it out as a datapoint ("holding made +5.0%, the strategy +1.2%"), never as a rule: a carry asset or a trending major can make a rotation look fine against zero while it trails holding.
2. **One verdict sentence.** `verdict` is one of `supported`, `weak`, `not_supported_by_backtest`, `no_backtest`, `pending`, and `reasons` names the rule that fired. Say the rule, not a feeling: "weak: only 1 of 3 out-of-sample folds is positive".
3. **Then the launch sentence, verbatim in spirit:** "You can still launch this in paper; the watchdog will report what it actually does."

For freestyle and path jobs the readout carries `performance_claim: null` and the fixed sentence "no backtest exists for this script; nothing here is a performance claim". Read that sentence, then show what the dry run did (`evidence.action_ledger_preview`: symbol, venue, filled/refused and why). A path component without a paper mode says so in `reasons`.

`refresh=true` on a harnessed job submits the backtest, the walk-forward holdout (last ~15% of bars as the test window) and the robustness check as heavy ops; on a hosted box they queue behind the live trading loop and run one at a time. The readout returns `pending` with `pending_ops` — queued and running ops alike — until they finish; you are prompted when each one does, and `op_status` (no faster than every 60 s) shows the queue position or progress. Never launch live while ops are pending.

Never name an infrastructure or data provider in the readout. Never say "backtested" about a freestyle script or a Path.
