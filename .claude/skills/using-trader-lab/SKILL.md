---
name: using-trader-lab
description: How to use Trader Lab — what consistently profitable Hyperliquid traders actually do (screened wallets by style, round trips with entry conditions, cohort flows, copyability, point-in-time cohorts) — to research and backtest the user's OWN strategies. Not a signals feed.
metadata:
  tags: wayfinder, trader-lab, hyperliquid, wallets, copy-trading, swing, dip-buying, momentum, backtest, research
---

## What you need to know (TL;DR)

**Trader Lab = research data on Hyperliquid's consistent winners, released daily (T+1).**

- Wallets are screened on **mark-to-market** P&L (realized − fees + Δunrealized + funding) and grouped into versioned **buckets**: `dip_buyer`, `momentum_swing`, `swing_other`, `scalper`, `mixed`, `hft_mm` (hidden by default), `unprofiled`.
- Every month-end a **point-in-time cohort** is picked using only data up to that date; `persistence` shows how those picks did afterwards. **Read it first** — it is the honest prior for everything else.
- It is **not** a signals product. Never tell the user to copy a wallet. Use it to find *conditions and behaviours* worth testing, then backtest the user's own rule.

```python
from wayfinder_paths.core.clients.TraderLabClient import TRADER_LAB_CLIENT as tl

meta = await tl.meta()                                   # release, coverage, persistence headline
board = await tl.leaderboard(bucket="dip_buyer", sort="sharpe", limit=20)
card = await tl.wallet(board["data"][0]["address"])      # style, markouts, copyability, equity
trips = await tl.trips(card["data"]["address"], limit=200)
rows = await tl.query(recipe="trips_with_features", params={"bucket": "dip_buyer", "coin": "ETH"})
```

**MCP quick access (≤50 rows):**

```
research_trader_lab_meta()
research_trader_lab_leaderboard(bucket="dip_buyer", sort="sharpe", limit="20")
research_trader_lab_wallet(address="0x…")
research_trader_lab_cohort(view="persistence")              # or copyability | entry_states (bucket=) | flows (coin=)
research_trader_lab_query(recipe="trips_with_features", params_json='{"bucket": "dip_buyer"}')
research_trader_lab_query(sql="SELECT bucket, count(*) FROM universe GROUP BY 1")
research_trader_lab_query_status(job_id="123")
```

**Bulk / local (optional extra `pip install 'wayfinder-paths[trader-lab]'`):**

```python
from wayfinder_paths.core.trader_lab import bulk, local, signals
d = await bulk.download_release(["round_trips", "entry_states", "cohort_membership"])
con = local.connect(d)          # the server's own views: SQL runs the same locally and remotely
```

## Costs (API credits; 1M credits = $0.10)

| Call | Credits |
|---|---|
| meta, datasets, recipes, schema, query status | 1 |
| leaderboard, wallet card, persistence | 1,000 |
| trips, flows, entry states, copyability | 2,000 |
| positions, markouts (run in the sandbox) | 5,000 |
| query | 600 per CPU-second, min 5,000; async +1M per GB of result |
| dataset file download | 3M per GB, min 50,000 |

Queries and downloads need an API key (they work from the SDK and Shells, not the web app). Re-downloads of unchanged files are free (sha256 cache). Prefer recipes over raw SQL; prefer one bulk download over many paged calls.

## When to use

- "What do the best Hyperliquid traders do?" / "How do dip buyers enter?"
- "Help me build a swing / momentum / dip-buying strategy from what works on Hyperliquid"
- "Would copying top traders work?" (answer from `persistence` + `copyability`, honestly)
- "Backtest buying ETH when it's down >1σ in an hour, like the dip-buyer cohort"

Not for: latency/HFT strategies (de-emphasised on purpose), live positions (data is T+1, no current positions), or wallet stalking.

## How to use

- [rules/what-is-trader-lab.md](rules/what-is-trader-lab.md) — datasets, buckets, columns, methodology
- [rules/workflow.md](rules/workflow.md) — **the 7-step research workflow** (follow it)
- [rules/pitfalls.md](rules/pitfalls.md) — look-ahead, survivorship, MTM, copy delay, costs
- [rules/recipes.md](rules/recipes.md) — server recipes, SQL patterns, local equivalents
- Example: `examples/trader_lab_dip_buyer.py`
