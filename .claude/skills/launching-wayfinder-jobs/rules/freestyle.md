# Freestyle jobs

Any trigger → any action, as a scheduled job with the same launch flow, watchdog and telemetry as a harnessed strategy. Create one with `core_jobs(action="create_freestyle", job_id=…, name=…, script_source="…", interval_seconds=…)` (or `script=` a path to a file). The module lands at `workspace/src/<job_id>.py`; the job is created paused.

## The contract

```python
from wayfinder_paths.jobs.freestyle import FreestyleSpec

SPEC = FreestyleSpec(
    venues=("polymarket", "hyperliquid"),
    max_notional_per_tick=500,
    max_loss_usd=25,
    halt_when={"max_drawdown": -0.15},
)

def tick(ctx):
    odds = ctx.quote("polymarket", "polymarket:<market-slug>:YES")
    ctx.state["last_odds"] = odds
    if odds > 0.6 and "BTC" not in ctx.positions:
        ctx.act({"venue": "hyperliquid", "kind": "market", "symbol": "BTC",
                 "side": "long", "notional": 200, "max_loss": 20})
    elif odds < 0.4 and "BTC" in ctx.positions:
        ctx.act({"venue": "hyperliquid", "kind": "close", "symbol": "BTC"})
```

- `tick(ctx)` is a plain function (not async). The runtime owns venues, the ledger, halts, risk checks, timeouts and recording.
- `ctx.quote(venue, symbol)` reads a price (HL mid, Polymarket CLOB mid). `ctx.positions`, `ctx.realized_pnl`, `ctx.mode`, `ctx.now`, `ctx.params` are reads.
- `ctx.act(action)` is the only trade seam. `kind` is `market | limit | buy` (open) or `close | sell | redeem` (reduce). Every open needs `notional` or `size`; put `max_loss` on it. It returns `ActionResult` (`filled | resting | rejected | refused`, with a reason). Halted jobs refuse openers and still allow exits.
- `ctx.state` is a dict that persists between ticks. `ctx.notify(title, body)` sends one notification per key per tick. `ctx.halt(reason)` latches the kill switch.
- `ctx.custom(label, coro)` is the escape hatch for venue calls the runtime cannot paper; it is skipped in paper mode and flagged (`custom_actions`).
- Supported venues: `hyperliquid`, `polymarket`, `hyperliquid_prediction`. On-chain swaps are not a venue yet; an `onchain` action is refused and validation reports it.

## Dry-run marks

The validation dry run quotes from stub marks. Pass them at creation: `create_freestyle(..., execution_params={"freestyle": {"validation_marks": {...}}, "initial_capital": 1000})`. Keys are `<venue>:<symbol>` (for a prediction market the symbol itself starts with `polymarket:`, so the key reads `polymarket:polymarket:<market>:YES`); unknown symbols quote 100 (perps) or 0.5 (prediction). A key `resolution:<venue>:<symbol>` settles that market at the given value from the second tick on, so a script can be seen buying on tick one and settling on tick two. Never put a real key or a wallet in execution_params.

## What validation checks

Static: `tick` present and sync, no direct venue writes (`hyperliquid_place_*`, `polymarket_*`, `swap_from_quote`, adapter imports…), no sleeps or `while True`, no forward recorder of its own. Then a **dry run**: three paper ticks in a subprocess with stub marks (override with `execution_params.freestyle.validation_marks`), an isolated forward directory and no network. The report's `freestyle.dry_run` lists every action and fill; the readout shows it.

## Paper, live, identity

Paper fills go through the venue's paper broker (taker fee and slippage assumptions per venue); live fills through the venue's real broker with the job's `wallet_label`. The ledger lives in `state/freestyle_ledger.json`; a mode flip archives it. Every tick compares the runner's baked revision with the workspace hash and refuses to run on drift. Live needs a wallet, `risk_limits.json` with a daily-loss or drawdown cap, `min_paper_runs` paper runs (default 20) and every warn flag acknowledged.
