# Research prior library

Use this as a treatment index, not as an answer generator. Start from the
attribution archetype or a clearly labeled prior-driven hypothesis, then use
the framework path in the last column.

| Family | Prior | Treats | Test path |
|---|---|---|---|
| Volume/participation (`volz:N`, dry-up, close-location rejection) | STRONG | noise stopout, trend fight | signal scan or campaign def |
| Candle path shape (sweep-and-reclaim, inside/narrow range) | STRONG intraday | adverse entry, noise stopout | signal scan or campaign def |
| Anchored levels (prior-day H/L, VWAP distance, round levels) | STRONG | adverse entry, early exit | signal scan or campaign def |
| Funding clock and funding-rate change | STRONG for perps | session anomalies | funding features + declared gate |
| Multi-timeframe alignment | STRONG | trend fight | causal resample + gate |
| Volatility term structure / event clocks | MODERATE | noise stopout, regime anomaly | campaign def + scenario |
| Correlation state / beta residual | MODERATE | trend fight | cross features + rank check |
| Cross-symbol structure (ratio z, lead-lag, breadth) | MODERATE | portfolio anomaly | pair/rank check + gate diagnostics |
| Exogenous market regime | STRONG for alts | regime anomaly | exogenous features + gate |
| Cross-venue basis / funding divergence | SPECULATIVE | execution anomaly | venue features + scenario |
| Exit structure (MFE target, trail, breakeven, scale-out) | STRONG when forensics support it | early exit | factorial grid + walk-forward |
| Sizing / leverage overlay | MODERATE | drawdown shape | multi-objective grid + walk-forward |
| Event aftermath / analogs of losers | SPECULATIVE | clustered failure | analog study, then declared scenario |

## Evidence tiers

- **Tier 1 — promote:** q<=%%T1_Q%% + %%T1_FOLDS%% folds,
  and the one-shot holdout. This is the only tier eligible for future live
  promotion.
- **Tier 2 — probation:** q<=%%T2_Q%% with %%T2_FOLDS%% folds and recent-half
  aliveness; or regime q<=%%T2_REGIME_Q%% with n>=%%T2_REGIME_N%% in the active
  regime; or a declared recent-window survivor at q<=%%T2_RECENT_Q%%. Limit to
  %%PROB_SIZE_PCT%% size. Max %%PROB_LEGS%% concurrent probation legs, with forward
  graduation and pre-registered kill criteria.
- Everything else remains research. Prefer declared regime conditioning over
  pretending all-history stationarity is required.

## Search routing

- Diagnose before treating. Rank ideas by prior strength, symptom match, and
  cost to test.
- Check the research agenda, candidate ledger, lineage, rejected proposals, and
  strategy library before inventing or resampling an idea.
- After %%STUCK_N%% consecutive neutral/hurt verdicts in the same family, jump to a new family, symbol,
  archived branch, or sizing/leverage axis.
- Use grid for small factorial attribution; use Optuna for wider mixed spaces;
  use return/drawdown multi-objective selection for structural risk changes.
- Development evidence cannot promote the change it helped select. Use reserved
  holdout, walk-forward, and the versioned robustness check for audit evidence.
- **Regime-aware aliveness:** judge a gated strategy inside the cells where its
  gate is active, not by calendar recency; deliberate inactivity outside those
  cells is not decay. A gate with zero observed activations is still unvalidated.
- Delegate heavy numeric work as a bounded question. Keep orchestration,
  framework interpretation, and proposal decisions with the parent agent.
