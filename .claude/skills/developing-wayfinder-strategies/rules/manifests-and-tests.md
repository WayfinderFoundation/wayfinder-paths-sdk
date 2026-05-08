# Manifests and tests (non-negotiables)

## Manifests are the source of truth

Each adapter/strategy directory has a `manifest.yaml` that must stay correct:
- Adapters: `entrypoint`, `capabilities`, `dependencies`
- Strategies: `entrypoint`, `name`, `permissions.policy`, `adapters`

Validation:
- Run `just validate-manifests` early and often.

## Strategy testing contract

For strategies in `wayfinder_paths/strategies/<strategy>/`:
- Maintain `examples.json` and load test inputs from it (never hardcode example values in tests).
- Provide smoke coverage for the lifecycle: `deposit → update → status → withdraw`.
- Optional read-only methods: `quote()`, `analyze()`, `build_batch_snapshot()` - implement these to support APY queries and batch scoring.

## Perp strategies: extra contract

For strategies that extend `ActivePerpsStrategy` (Hyperliquid perps, etc.):

- The class must declare `name`, `REF`, `SIGNAL`, `DECIDE`, `HIP3_DEXES`, `DEFAULT_PARAMS` — see [reference-strategies.md](reference-strategies.md).
- `signal.py::compute_signal` must return a `SignalFrame`, not a raw DataFrame.
- `backtest_ref.json` must be schema-compliant with `wayfinder_paths/core/backtesting/ref.py::BacktestRef`. Required: `produced`, `code{signal, decide}`, `venues`, `data{symbols, interval, window, fingerprint}`, `params`, `execution_assumptions`, `performance`, `monitoring`.
- **`execution_assumptions.slippage_bps` should reflect real live costs**, not a tiny default. The canonical reference (apex_gmx_velocity) calibrates from a real-fill audit; expect 15–30 bps on HL HIP-3-tier orderbooks. Using 1 bps inflates reported Sharpe and gets unwound the moment reconcile runs against live data.
- Provide a parity test: signal output vs the reference implementation it derives from must be byte-identical (max abs diff < 1e-9). The canonical reference includes one — copy the pattern.

**Always start a new perp strategy by copying `apex_gmx_velocity/` and modifying.** Don't construct from scratch — you'll silently miss the SignalFrame contract, the backtest_ref schema, the size-rounding gotcha, or the per-bar `iloc[-1]` lookup pattern.

## Adapter testing contract

For adapters in `wayfinder_paths/adapters/<adapter>/`:
- Cover key read paths with mocked clients.
- If there are execution methods, ensure tests mock the underlying clients.

