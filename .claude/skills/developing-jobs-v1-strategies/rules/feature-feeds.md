# Feature feeds: token prices, DeFi yields, funding, hand-published rows

A harnessed strategy sees only its venue candles unless a **feature feed** carries more. A feature is a series of rows in `state/features.jsonl` (`{timestamp, name, value, symbol, written_at}`) declared in `execution_spec.data_contract.features`; the dataset loader (backtest) and the live driver merge every declared feature onto the bars **as-of backward** — a bar sees the latest row at or before its own timestamp, never a future one — with identical semantics, so backtest/live parity holds by construction. `symbol: null` rows are global (every traded symbol sees them); rows with a symbol are per-symbol.

The feature **schema** is revision-bound: declaring, re-declaring or changing a feature's cadence or smoothing is a strategy change and re-gates promotion like any code edit. Feature **data** lives outside the revision hash, so refreshes never re-gate. After a fetch that declares, validate again before launch.

## The verbs

| verb | what it writes | name |
|---|---|---|
| `fetch_funding` (`wayfinder job fetch-funding`) | perp funding settlements per symbol | `funding` |
| `fetch_token_features` (`fetch-token-features`) | an on-chain token's USD price history, coarsened to the bar interval, pinned to chain and address | `token_price:<token_id>` |
| `fetch_yield_features` (`fetch-yield-features`) | DeFi yield history by feed name, pinned to the yield service's ids | `lend_supply_apr:<venue>:<symbol>[:<market>]`, `lend_borrow_apr:…`, `yield_apy:<symbol>`, `pendle_implied_apy:<venue>:<market_id>`, `boros_fixed_rate:<venue>:<market_id>` |
| `wayfinder job feature append` | one hand-published row (research conclusions, briefs) | anything else |

Examples: `core_jobs(action="fetch_token_features", job_id=…, token_ids=["ethereum-base", "polygon_0x…"])`; `core_jobs(action="fetch_yield_features", job_id=…, feeds=["lend_supply_apr:aave-base:USDC", "yield_apy:sUSDe"], smoothing="mean:24h")`. Token ids are `<coingecko_id>-<chain_code>` or `<chain_code>_<address>`. Venue names carry their chain suffix (`aave-base`, `morpho_ethereum`); a venue with several markets for the symbol needs the fourth part (`:<market_external_id>`) — the error names the choices. Discover venues and markets with `research_search_lending`, `research_search_delta_lab_markets` and `research_search_delta_lab_instruments`; `core_jobs(action="status")` lists what a job declares under `features`. Default history is the dataset's own span; yields keep about seven months, and a longer dataset carries `None` before that (the backtest summary's coverage note says so).

## Declaration fields

Each declared entry carries `name`; `feed` (the pinned source ids the verbs wrote — never hand-edit them); `cadence` (the feed's native period: the candle interval, `1h` for hourly yields, `1d` for a daily series); `smoothing` (`{"method": "none"|"mean"|"ewm", "window": "24h"}` in feed time); `max_age_seconds` (default three periods) and `stale_policy` (`skip` skips the tick with a `stale_feature` guard, `decide_anyway` decides and records the guard); optional `column` to alias a long name.

## Cadence, smoothing, reconciliation

Feeds arrive on their own clocks against 5-minute bars. With a `cadence`, reads keep one observation per period (the last, at its real observation time, so nothing is seen early), and a 5-minute strategy on an hourly feed sees a step once an hour by design. `smoothing` softens the steps: `mean` is a trailing mean over the window, `ewm` an exponential mean with the window as half-life, both computed from feed rows only and applied in the merge, so backtest and live carry the same column; the unsmoothed value stays available as `<column>__raw`. Yields default to a trailing day (`mean:24h`), the number a lender quotes; prices default to `none`.

Sources restate recent values. The hourly wake refresh re-fetches a two-period overlap, re-appends any restated value (reads take the last written row, so the revision wins everywhere) and journals `feed_revised`. Gaps wider than two periods are never silently held: they surface as `feed_gap` guard events live and as `gaps` in the backtest coverage.

## Reading a feature

`ctx.view.feature("token_price:ethereum-base")` returns the latest non-null value on the current bar and raises `ValueError` before the first row — catch it and return `[]`. Precompute frames carry every feature column too, so indicators can build on them. Units: prices in USD; every rate a decimal per year (0.05 is 5%) except `funding`, which is per settlement; Pendle implied APY and Boros fixed rates are market-implied, not realised.

## Freshness

Declared token and yield feeds refresh on the wake's hourly stamp (`results/research/derived_refresh.json`), incrementally from the newest stored row. A failing feed journals `derived_features_refresh_failed` and, after three in a row, `data_feed_degraded`; recovery journals `data_feed_recovered`. `status` shows each feature's newest stamp, age, gaps and revisions.

## The `funding` name is special

A feature named exactly `funding` is also charged against open positions as funding cashflow in the simulator. Never reuse that name for a yield.
