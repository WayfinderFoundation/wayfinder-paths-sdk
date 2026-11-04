# Recipes and SQL

Recipes are parameterised server queries (cheap, validated). List them with `await tl.recipes()`; schema with `await tl.schema()`.

| Recipe | Params |
|---|---|
| `wallet_trips` | address, coin?, since?, until?, limit≤10k |
| `wallet_positions` | address, coin?, since?, until?, limit≤10k |
| `wallet_markout_curve` | address, coin? |
| `bucket_entry_states` | bucket |
| `coin_flow_hourly` | coin, bucket?, since?, until?, limit |
| `trips_with_features` | bucket?, coin?, since?, until?, limit |
| `pit_cohort_returns` | as_of (month end), selector (all/top50_sharpe/top50_net), until? |
| `copy_sim` | address?, bucket?, delay (1/5/15), cost_bp (5/11/20), limit |
| `bars` | coin, since, until?, limit≤10k |

Dates are `YYYY-MM-DD` (UTC).

## Raw SQL (one SELECT over the views; sync ≤20 s / 10k rows, async ≤300 s / 1 GB)

```python
# Win rate and median return of dip-buyer longs by 1h z-score bucket
await tl.query(sql="""
  SELECT round(e.z60 * e.side * 2) / 2 AS z60_signed, count(*) AS n,
         avg((e.ret_bp > 0)::INT) AS win_rate, median(e.ret_bp) AS med_ret_bp
  FROM entry_states e JOIN universe u USING (address)
  WHERE u.bucket = 'dip_buyer' AND e.side = 1
  GROUP BY 1 HAVING count(*) >= 50 ORDER BY 1
""")

# Big result → async, then download Parquet
job = await tl.query(sql="SELECT * FROM round_trips WHERE coin = 'ETH'", mode="async")
done = await tl.wait_for_query(job["data"]["id"])
path = await tl.download_query_result(job["data"]["id"], Path("eth_trips.parquet"))
```

Not allowed in SQL (rejected): anything other than a single SELECT, file/URL access (`read_*`, paths, `glob`), settings, `SHOW`/`DESCRIBE` (use `tl.schema()`), parameters.

## Locally (same SQL)

```python
from wayfinder_paths.core.trader_lab import bulk, local, signals
d = await bulk.download_release(["entry_states", "universe", "cohort_membership", "round_trips"])
con = local.connect(d)
df = con.sql("SELECT * FROM entry_states WHERE coin = 'ETH'").df()

# Point-in-time cohort → late-copy weights → backtest
members = signals.pit_cohort(con, "2026-06-30", selector="top50_sharpe")
trips = con.execute("SELECT coin, t0, t1, side FROM round_trips WHERE address IN (SELECT unnest(?)) AND t0 > DATE '2026-06-30'", [members]).df()
weights = signals.trips_to_weights(trips, prices.index, delay=pd.Timedelta(minutes=5))
```
