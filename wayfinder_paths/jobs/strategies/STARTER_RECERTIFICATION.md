# Starter catalogue recertification — September 2026

Status: draft, pending receipt recovery and current-base historical evaluation.
This updates existing starters and their cards, not a separate catalogue.
No deployment or live promotion has occurred.

## October 1 base update

The catalog-only branch starts at `wayfinder-jobs-v1` commit `42532ca9`, including
the bounded-window and forward-parity changes in #834. It does **not** bring
the older research branch's gate-policy, feed-replay or simulator changes into
production. The results below remain recorded evidence from evaluation SDK
`5d465ec483f3d36ff64d27c832ed07665c52c60e`, not new-base certification.

Five starters remain selectable; thirteen are retired from new selection and
evolution seeding. Funding/OI Divergence is among the retired entries because
its recertification was unfinished, not because a completed test disproved it.

Compatibility changes and checks:

- Keep exact evaluated parameters (`exit_rsi: 60`, not a newly hashed `60.0`).
- Use each card's taker costs on new jobs, including 7 bps slippage.
- Fetch 120 days plus the declared warmup history, rather than consuming part
  of the evaluation window with warmup.
- Preserve all stable IDs and existing job parameters, scripts and recorded
  cards; the revised trend uses a separate module. Shared independent-leg exit
  handling is corrected so a missing peer does not suppress a healthy exit.
- Test bounded indicators, actual per-tick live-mode replay, and evolution's
  rescaling of the new 20-day volatility window from 15m to 5m bars. Synthetic
  compatibility tests require nonzero decisions; they are not profit evidence.

Before merge: recover the original raw receipts and frozen data from the paths
below (currently absent), verify their hashes, then replay the five fixed
revisions and the production 30-day parity check on the current engine. Do not
silently carry historical gate passes across SDK revisions or replace receipt
hashes with hashes of newly generated artifacts.

## Decision rule

The assistant authors a diagnosed hypothesis, tests it with the shared execution
engine, selects a fixed revision, and requires that revision to pass the agreed
quick screen, full development and historical paper-admission checks. Only then
do its starter parameters/implementation and card evidence change together.
An original can remain when a proposed repair fails, provided the original
itself passes. A starter without a qualified version is hidden from new
selection and evolution seeding, not deleted from existing jobs.

There are no DeepSeek sessions or autonomous candidate-generation runs in this
work. The local harness performs deterministic evaluation only.

## Evidence and policy

The verified historical year ends on September 27, 2026. Strategy repairs are
compared on three chronological tests within its first 80%; parameter grids
select on the preceding training slices. Every selected grid revision below
was the same winner in all three folds. The outer July–September validation
and final paired evidence remain necessary admission checks, not tuning data.

This history has been inspected repeatedly. It is research evidence, not an
independent sealed holdout, and none of these results is a forward probation.
The recorded comparison floor is 104 for updated baselines and 132 for the
declared repair round; the search count is not reset for a favourable result.

The explicitly user-approved `starter-paper-2026-09` profile uses:

- Cost coverage 1.25× and maximum quick-slice loss 4%.
- Full-development trial haircut reported rather than blocking.
- Tail metric retained, with zero utility weight and no fixed tail-sum ceiling.
- Recent seven-day utility weakness reported as a paper warning, not a veto.

Other economic, activity, execution and risk checks remain. The reference in
this catalogue audit is cash, not a user's incumbent. Qualification is at 1×;
the leverage sweep is risk information, not qualification at higher leverage.
Production governance defaults, owner authority and live promotion are not
changed by this catalogue refresh. These distinctions are also in card data.

## Recorded historical selections

All figures below are continuous shared-engine historical returns after fees,
slippage and verified funding, not hand-reconstructed P&L.

| Existing starter | Decision | Year return | Sharpe | Max drawdown |
|---|---|---:|---:|---:|
| Diversified Trend Sleeves | Inverse 20-day relative-volatility sizing; 35% sleeve cap | 79.81% | 2.68 | 9.41% |
| Balanced Passive Capitulation | Exit RSI 50 → 60 | 26.47% | 2.75 | 4.02% |
| Mixed Volume Capitulation | Exit RSI 50 → 60 | 16.87% | 2.58 | 2.24% |
| Mixed Bollinger Pullback | Retain qualified 12-hour original; reject 36-hour revision | 4.50% | 1.17 | 1.75% |
| Mixed Sleeve Momentum | Retain qualified original; reject risk-balancing revision | 29.35% | 1.27 | 15.47% |

The trend change improved mean training-test return/Sharpe/worst drawdown, but
was weaker on the outer validation than the original and has a negative recent
week warning. The passive change improved the training-test comparison and
annual return, but increased annual drawdown and reduced annual Sharpe. Neither
is described as an improvement in every period or every metric.

Funding/OI original vital statistics and the remaining repair comparisons were
unfinished when work stopped; no evaluation is currently running. Unqualified
or unsuccessful originals remain hidden. An improved version must qualify
before its card returns.

## Integrity and backwards compatibility

Cards replace stale revision-specific performance claims rather than merging
new numbers into old evidence. They record data, implementation-AST, parameter
and receipt hashes. Tests compare the code/parameters a new job launches with
those hashes and check that default stats equal the 1× leverage row.

The revised trend starter selects a separate implementation module so changing
its catalogue entry does not change older jobs importing the original module.
Parameter-only changes affect new jobs; old jobs retain their stored parameters
and original card evidence. Retirement also preserves stable-ID lookup and
existing-job reopening.

The original report recorded reproducibility artifacts under
`.wayfinder_runs/research/starter_recertification/`: `catalog-manual-studies-20260927-v1`,
`catalog-revisions-20260927-v1`, `catalog-vitals-20260927-v1`, and
`recent-week-warning-reassessment-v2`. Those paths are currently absent; the
cards retain the recorded hashes, but hash strings alone do not recover or
independently verify the receipts. This is why the refresh remains a draft.
