# Research prior library — idea families, their priors, and how to test them

Methodology, not answers: each family says HOW to look and WHICH tool
adjudicates. Prior strength = how much benefit of the doubt a hypothesis from
this family earns in triage (STRONG priors are well-documented market
effects; SPECULATIVE ones need a symptom match to justify the spend). Every
family lists the failure archetypes it treats — start from the attribution
block's archetype counts and pick the family that treats YOUR disease.

| Family | Prior | Treats | Test path |
|---|---|---|---|
| Volume/participation: `volz:N` at signal time (real break vs dead tape), volume dry-up before compression, `clv` rejection closes | STRONG | noise_stopout, trend_fight | campaign workspace defs / `signal-check --column` |
| Candle path-shape: `wickratio` sweep-and-reclaim (wick through a prior level, close back inside), inside-bar/narrow-range runs | STRONG intraday | noise_stopout, adverse_entry | campaign workspace defs |
| Anchored levels: `daylevel` (prior-day H/L), `vwapdist`, round numbers | STRONG | adverse_entry (entries far from anchors), early_exit (targets at anchors) | campaign workspace defs |
| Funding-settlement clock (`fundclock`) + funding rate-of-change columns | STRONG (perp-documented) | session-shaped anomalies in attribution | `fundclock` spec + derive-features funding set |
| MTF alignment: 4h/1d state gating lower-TF entries | STRONG | trend_fight | resampled workspace defs (the 30m-on-5m pattern) |
| Vol term structure & event clocks: `rvratio:N:M` adaptive squeeze, `sigmabars:K` clustering timer, post-cascade windows | MODERATE | noise_stopout, regime-slice anomalies | specs + campaign defs |
| Correlation state: idiosyncratic vs beta moves via `corr_*` columns | MODERATE | trend_fight (fading beta moves) | derive-features cross set + `--column` |
| Cross-symbol structure: `ratioz_*` pair reversion, lead-lag via `panelret_lag1`, `breadth_sma*` gates | MODERATE | portfolio-level anomalies | derive-features cross set + rank-check (research — NOT gated by the forward-trade floor) |
| Exogenous regime: `btc_trend`/`btc_ret*` columns | STRONG for alts | regime-slice anomalies | derive-features exog set |
| Cross-venue basis: `venue_basis_bps`, funding divergence | SPECULATIVE | execution-quality anomalies | derive-features venue set |
| Exit-structure alpha: MFE targets, trailing, breakeven, scale-out — pre-registered strategy params (`mfe_target_bps`, `trail_bps`, …) in decide(), no engine change | STRONG when forensics show avg MFE >> avg realized | early_exit | compound factorial grid + WF |
| Sizing overlays: vol-scaled legs, signal-strength scaling, regime-conditional size — pre-registered params | MODERATE | drawdown-shape anomalies | factorial grid + WF |
| Event-aftermath / analogs-of-losers: what do the nearest analogs of each loser's pre-entry window share? | SPECULATIVE (ideation fuel) | any clustered archetype | `analogs` + `chart` lenses → then a campaign def |

## Evidence tiers

- **Tier 1 — promote** (gates unchanged: pooled q<=0.10 + 3/4 folds +
  one-shot holdout): full-size leg, and the only tier eligible for any
  future LIVE promotion.
- **Tier 2 — probation** (any of: q<=0.20 + 2/4 folds + edge alive in the
  recent half; regime-conditional q<=0.15 with n>=20 in the CURRENT regime;
  recent-window family survivor at q<=0.10): deployable at <=50% leg size
  with PRE-REGISTERED graduate + kill criteria. Paper forward is the
  holdout — the tier exists because a paper false positive costs nothing
  but attention, while burying every conditional edge costs the book its
  reason to exist. Max 2 concurrent probation legs per job; regime flip =
  kill trigger; graduation from FORWARD trades only.
- Everything else stays research. Prefer testing `signal | regime`
  (`--condition-regime`) over demanding all-history stationarity; use
  `--window-days N` for declared recent-window families.

## Protocol reminders

- **Diagnose before treating**: every hypothesis cites the attribution slice
  or archetype count it treats, OR is explicitly labeled a prior-driven bet.
- **Triage**: rank by prior strength × symptom match × cost-to-test. Each
  ideation allocates a portfolio: >=1 cheap test, >=1 structural, <=1
  moonshot, and >=1 family not yet in the dead map.
- **Campaigns**: new-def sweeps run as `signal-scan --campaign NAME` — your
  declared defs are their own BH family; the canonical library stays
  untaxed. Declare the campaign's hypothesis families in the agenda BEFORE
  scanning; renaming to relaunch is snooping (the ledger records it).
- **Compound experiments** (second-order treatments): express a
  multi-intervention causal story as 2-4 pre-registered factors (boolean
  gates / structural params), run the factorial via the experiments grid.
  Box discipline is TWO-STAGE: screen the full factorial with
  `--workers 1 --quick 10000`, then full-history + walk-forward on ONLY the
  winning cell and its one-factor neighbors. Cite `factor_attribution` in
  the proposal; a factor with a negative marginal effect does not ship
  unless a documented sign_flip interaction is the finding.
- **Pre-mortem + kill criteria**: every proposal states its expected new
  failure mode and a pre-registered kill/re-arm threshold in the intent
  contract (the POL funding-gate pattern, generalized).
- **Dead-map scope**: dead = the tested claim, never the asset or family;
  owner rejections bind on the change, not its neighborhood.
- **Long runs via CLI in-session (detached with a log), never MCP ops** —
  the 300s op timeout cannot hold a grid.
