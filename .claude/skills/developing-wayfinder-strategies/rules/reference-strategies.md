# Reference strategies

When you build a new strategy, copy the layout and contracts from the
canonical reference for that style. Don't invent new patterns.

## Perp strategies (Hyperliquid, ActivePerps base)

**Canonical reference:** [`wayfinder_paths/strategies/apex_gmx_velocity/`](../../../../wayfinder_paths/strategies/apex_gmx_velocity/)

Why this one:
- Inherits from `ActivePerpsStrategy` (the right base for perp/HL strategies)
- Clean separation between `signal.py` (pure target weights) and
  `decide.py` (per-bar order placement)
- `backtest_ref.json` is schema-compliant with the parser in
  `wayfinder_paths/core/backtesting/ref.py`
- Slippage assumption (25 bps) reflects what live actually costs on
  HIP-3-tier orderbooks — calibrated from a real-fill audit
- Reconcile-ready: signal returns `SignalFrame`, decide rounds sizes
  via `round_size_for_asset()`, snapshots will populate orders+signal_row
  on every trigger
- Backtest reproduces the audit's gen_velocity reference signal exactly
  (parity verified)

### Files and their roles

```
strategies/apex_gmx_velocity/
├── signal.py              Pure: prices → SignalFrame(targets DataFrame)
├── decide.py              Per-bar: signal + handler state → place_order calls
├── strategy.py            Class declaration: REF, SIGNAL, DECIDE, DEFAULT_PARAMS
├── manifest.yaml          Adapter requirements + params + risk limits
├── backtest_ref.json      Frozen reference: code SHAs, params, performance,
│                          drift tolerances. Reconcile compares against this.
├── risk_limits.json       Halt thresholds (drawdown, gross, daily loss, etc.).
│                          REQUIRED — without it, the strategy runs uncapped and
│                          ActivePerpsStrategy.update() emits a risk_warning.
├── examples.json          Test fixture + expected backtest ranges
├── README.md              Performance, deploy steps, funding economics
└── test_strategy.py       Smoke: class wires, signal invariants, backtest reproduces ref
```

### Wallet-readiness gotchas (deploy blockers)

Before the runner can actually trade, two wallet conditions must hold. Missing either one means the strategy *runs* but does *nothing useful*:

1. **Wallet label in `config.json` matches the strategy class's `name`.**
   `get_adapter()` looks up the wallet by exact label string. Mismatch → `Wallet '<name>' not found.`

2. **`risk_limits.json` exists in the strategy directory.**
   Without it, `ActivePerpsStrategy.update()` runs but emits a `risk_warning` in every status response and runs *uncapped* — no drawdown halt, no per-symbol cap, no daily-loss halt. See "Risk limits" below for the schema.

### Contracts the SDK enforces

1. **`signal.py::compute_signal(prices, funding, params) -> SignalFrame`**
   - Must return `SignalFrame(targets=df)`, NOT a raw DataFrame
   - `df` columns must include all symbols in `prices.columns`
   - `df` values are target weights (already-leveraged convention) per symbol per bar
   - Sum of `|weights|` per row must be ≤ `target_leverage`

2. **`decide.py::decide(ctx: TriggerContext) -> None`**
   - Read targets via `ctx.signal.targets.iloc[-1]` (live `ctx.t` is wall-clock,
     doesn't align to bar index — exact-match `.loc[t]` will fail)
   - Read NAV via `ctx.nav` — framework-owned, identical in backtest and live.
     **Never** call `await ctx.perp.get_margin_balance()` or `ctx.state.set("nav", ...)`
     from inside decide. Backtest's `BacktestHandler.get_margin_balance()` returns 0,
     and any stored NAV gets pinned to first-observed truth in live (canonical bug).
   - Read positions via `await ctx.perp.get_positions()`
   - Round order size via `round_size_for_asset(adapter.asset_to_sz_decimals,
     asset_id, raw_size)` — HL signing rejects floats with too many decimals
   - Place orders via `await ctx.perp.place_order(sym, side, size, "market", ...)`
   - End with `await scale_pending_atomically(ctx, leverage=...)`

3. **`strategy.py::<Cls>(ActivePerpsStrategy)`**
   - Class-level `name`: must match a wallet label in `config.json`
   - Class-level `REF`: `Path(__file__).parent / "backtest_ref.json"`
   - Class-level `SIGNAL` / `DECIDE`: dotted `"module:attr"` strings
   - Class-level `DEFAULT_PARAMS`: dict including `symbols`, `target_leverage`,
     `min_order_usd`, `rebalance_threshold`, plus strategy-specific knobs
   - Don't override `update()` or `_run_trigger()` — they're locked

4. **`backtest_ref.json` schema** (parsed by `core/backtesting/ref.py`)
   - Required top-level keys: `schema_version`, `produced`, `code`,
     `venues`, `data`, `params`, `execution_assumptions`, `performance`, `monitoring`
   - `code.signal` and `code.decide` need `module`, `entrypoint`, `source_sha256`
   - `data.window` needs `start`, `end`, `bars`
   - `data.fingerprint` is required (the parser doesn't tolerate missing it)
   - `execution_assumptions.slippage_bps` should reflect realistic costs
     (1 bps is unrealistic for HL HIP-3-tier orderbooks; calibrate from
     real fills via `reconcile`'s slippage axis)
   - `performance.sharpe`, `total_return`, `max_drawdown` populate `quote()`

### Risk limits

`risk_limits.json` lives next to `backtest_ref.json` and is loaded by
`RiskLimits.load_optional` in `wayfinder_paths/core/strategies/risk_limits.py`.
Schema (all fields optional, but file presence is required to avoid the
`risk_warning` and to actually halt on breach):

```json
{
  "max_drawdown": -0.20,                    // negative decimal
  "max_gross_exposure_usd": 200.0,
  "max_position_per_symbol_usd": 100.0,
  "max_daily_loss_usd": 10.0,
  "pause_after_consecutive_losses": 5,
  "min_rolling_30d_sharpe": 0.5,            // optional; needs ~30d live first
  "_notes": {                                 // free-form, ignored by parser
    "rationale": "...",
    "max_drawdown_basis": "..."
  }
}
```

`ActivePerpsStrategy.update()` checks limits at the top and returns
`(False, "Halted: <reason>")` rather than raising on breach. **Calibrate
to the wallet's NAV and the backtest's worst-case drawdown — don't copy
the reference's numbers blindly.**

### Gotchas the reference avoids

- **Empty `__init__.py` re-exports**: Don't `from wayfinder_paths.core.backtesting import run_backtest` — import from the submodule (`backtester.run_backtest`) since the package init was deliberately emptied.
- **`find_strategy_class` parent-class bug**: The CLI uses module-introspection to find the strategy class. If your `strategy.py` imports `ActivePerpsStrategy` at module scope (it must), the resolver could pick the parent. Fixed in `wayfinder_paths/run_strategy.py:57+` to filter framework bases — but if you need to bypass, the manifest entrypoint always works.

### When to deviate from the reference

You can safely deviate on:
- The signal logic (RSI, momentum, vol-zscore, multi-pair, basket — whatever)
- Number of pairs / symbols
- Rebalance threshold, lookback, entry threshold
- Risk limits

Don't deviate on:
- File names or roles (signal.py / decide.py / strategy.py / manifest.yaml / backtest_ref.json)
- Class hierarchy (extend `ActivePerpsStrategy`)
- Signal contract (returns `SignalFrame`)
- backtest_ref.json schema
- Snapshot capture (handled automatically by `_run_trigger` if you use the recommended decide pattern)

### Validation checklist before claiming the strategy is done

```
[ ] Strategy class loads:
    poetry run python -c "from wayfinder_paths.strategies.<name>.strategy import *; print('ok')"

[ ] Ref parses:
    poetry run python -c "from wayfinder_paths.core.backtesting.ref import load_ref; load_ref('wayfinder_paths/strategies/<name>')"

[ ] risk_limits.json exists and has at least max_drawdown + max_daily_loss_usd
    set to wallet-appropriate values. Verify it loads:
    poetry run python -c "from wayfinder_paths.core.strategies.risk_limits import RiskLimits; print(RiskLimits.load_optional('wayfinder_paths/strategies/<name>'))"

[ ] Wallet exists with label == strategy.name and has USDC in HL PERP
    (not just spot). Use core_get_wallets(label="<name>") to verify both;
    if spot-only, run hyperliquid_execute(action="spot_to_perp_transfer", ...)
    before the first update.

[ ] CLI status returns clean StatusDict (no risk_warning):
    poetry run python -m wayfinder_paths.run_strategy <name> --action status \
      | jq '.strategy_status.risk_warning'   # should be empty string

[ ] CLI quote returns expected_apy:
    poetry run python -m wayfinder_paths.run_strategy <name> --action quote

[ ] Signal parity vs backtest reference code (gen_velocity or whatever):
    Write a parity script under .wayfinder_runs/.scratch/ that asserts
    max |strategy_sig - reference_sig| < 1e-9 over the audit window.

[ ] Backtest runs through wayfinder_paths.core.backtesting.backtester.run_backtest
    with the strategy's signal output and reproduces ref performance within
    a stated tolerance.

[ ] Smoke test passes: pytest wayfinder_paths/strategies/<name>/test_strategy.py -m smoke
```

apex_gmx_velocity passes all ten checks. Use it as the template.
