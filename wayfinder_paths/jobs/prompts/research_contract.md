# jobs-research-contract-v1

This contract is stable across job-worker wakes and compaction.

- Framework execution stats, fills, fees, funding, and drawdowns are the source
  of truth. Never recreate strategy PnL with a separate scratch model.
- Strategy features are causal and vectorized in `precompute`. Observable
  decision gates use `gate_*`; `decide()` consumes the same column the trace
  audits.
- Zero gate activations are unvalidated (`gate_unobserved`), not a pass.
- Positive perp funding means longs pay shorts. Funding-dependent claims require
  adequate span and symbol coverage; otherwise mark `funding_incomplete`.
- Run `robustness_check` before recommending deployment. Review neighboring
  parameters, rebalance phase, leverage, walk-forward decay, declared scenarios,
  and paired candidate-versus-incumbent results as applicable.
- Recent/event windows used to generate or select a candidate are development
  evidence and cannot also serve as its audit holdout.
- Robustness is advisory in contract v1. It enriches candidate evidence but does
  not change or bypass the existing approval gate.
- The parent strategy/job agent orchestrates and decides. Quant delegation is
  bounded to supplied artifacts and questions; it must not recreate the
  execution engine or promotion policy.
