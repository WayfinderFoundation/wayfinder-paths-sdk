# The honest readout

`core_jobs(action="readout", job_id=…)` writes `reports/readout/latest.json` and returns it. Read it back to the user in this shape, in this order:

1. **Money first, in plain words.** For a harnessed job: net return after costs on the backtest window, the chronologically last walk-forward fold (the small holdout) on its own, how many out-of-sample folds were positive, how much of the in-sample return survived (`decay_ratio`), whether replication on the refreshed dataset still holds, and how much of gross profit fees take. Quote the numbers from `evidence`; do not round them into adjectives.
2. **One verdict sentence.** `verdict` is one of `supported`, `weak`, `not_supported_by_backtest`, `no_backtest`, `pending`, and `reasons` names the rule that fired. Say the rule, not a feeling: "weak: only 1 of 3 out-of-sample folds is positive".
3. **Then the launch sentence, verbatim in spirit:** "You can still launch this in paper; the watchdog will report what it actually does."

For freestyle and path jobs the readout carries `performance_claim: null` and the fixed sentence "no backtest exists for this script; nothing here is a performance claim". Read that sentence, then show what the dry run did (`evidence.action_ledger_preview`: symbol, venue, filled/refused and why). A path component without a paper mode says so in `reasons`.

`refresh=true` on a harnessed job starts the backtest, the walk-forward holdout (last ~15% of bars as the test window) and the robustness check as detached ops; the readout returns `pending` with `pending_ops` until they finish (`op_status` polls). Never launch live while ops are pending.

Never name an infrastructure or data provider in the readout. Never say "backtested" about a freestyle script or a Path.
