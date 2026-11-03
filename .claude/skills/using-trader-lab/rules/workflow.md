# Research workflow (follow in order)

The goal is a rule the user owns and has tested — never "follow wallet X".

1. **Check persistence.** `research_trader_lab_cohort(view="persistence")`. Report how often top picks stayed profitable out of sample (e.g. "74% of the top 50 by Sharpe were still profitable 3 months later; the median out-of-sample Sharpe was 0.9"). This sets expectations for everything after.
2. **Pick a style and read the leaderboard.** `research_trader_lab_leaderboard(bucket=…)`. Look at 3–5 wallet cards (`research_trader_lab_wallet`): hold times, win rate vs payoff, long share, markouts (is the edge in timing or in holding?), coin mix.
3. **Compare entry states with the baseline.** `research_trader_lab_cohort(view="entry_states", bucket=…)`: where the cohort's entries differ from "any time" is the candidate edge (e.g. dip buyers enter after 1h z ≤ −1 far more often than chance).
4. **Pull trips with features.** `tl.query(recipe="trips_with_features", params={"bucket": …})` or download `entry_states` and work locally. Check how outcomes (`ret_bp`, `net`) vary with the features.
5. **Write the user's own rule** from step 3–4 (e.g. "long ETH when z60 ≤ −1.2 and funding ≤ 0; exit after 6h or −150 bp"). Keep it simple; one or two conditions.
6. **Backtest on point-in-time data** with `/backtest-strategy` at **≥11 bp round-trip cost**. If the rule depends on which wallets you learned from, rebuild it per vintage (`signals.pit_cohort`) and test only on the months after each vintage.
7. **Compare with copyability.** `research_trader_lab_cohort(view="copyability")`: if copying the cohort 5 minutes late at 11 bp is weak, say so — and say whether the user's rule does better.

Report findings with numbers, caveats from `pitfalls.md`, and the release you used.
