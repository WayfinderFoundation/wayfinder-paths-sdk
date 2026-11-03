# Pitfalls

- **Look-ahead.** Today's leaderboard was picked with hindsight. Any backtest that trades "wallets that look good now" over past dates is invalid. Use `cohort_membership` vintages and test only after each `as_of`.
- **Survivorship and selection.** The universe is, by construction, wallets that did well. Expect regression to the mean — that is what `persistence` measures. Never present screened returns as achievable.
- **MTM vs realized.** Use `net_mtm`. `net_realized` hides open losers.
- **Copying late costs the edge.** Fast styles (scalpers, makers) lose most of their edge at 1–5 minute delay. Check `copyability` before suggesting anything copy-like; at 11 bp cost many verdicts are `weak`.
- **Costs.** Backtest at ≥11 bp round trip (taker + slippage) unless the rule is genuinely maker-only.
- **Illiquid coins.** `illiquid` wallets trade thin coins; their returns may not scale. Filter on liquid coins for strategies.
- **HFT / market makers** are hidden by default (`include_hft`). Their P&L comes from rebates and latency the user cannot replicate.
- **T+1, no live positions.** Data ends the day before release; there are no current positions by design. Don't present it as "what they are doing now".
- **Small samples.** Treat wallets with <40 trips, and bins with few entries, as anecdotes.
- **Release drift.** Bucket rules are versioned; compare like with like (`rules_version`). Quote the `release` in results.
- **Privacy.** Addresses are public on-chain, but don't speculate about who is behind a wallet. Respect opted-out wallets (they disappear from every view).
