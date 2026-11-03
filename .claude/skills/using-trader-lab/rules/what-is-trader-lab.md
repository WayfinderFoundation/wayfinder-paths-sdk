# What Trader Lab is

Daily (T+1) releases built from Hyperliquid perp fills, end-of-day positions and 1-minute bars. Each response carries `release` (build date), `as_of` (data runs through this UTC day) and `rules_version` (bucket rules).

## Screen

A wallet enters the **universe** with ≥40 active days over ≥4 months, ≥60% positive weeks and daily MTM Sharpe ≥2. P&L is **mark-to-market**: realized − fees + change in unrealized + funding, from end-of-day snapshots. `net_realized` is kept for comparison; never screen on it (it flatters holding losers).

The **cohort** (detailed data) is the universe minus HFT, capped at 2,000 wallets by Sharpe; wallets with >2M fills get aggregates only.

## Buckets (rules_version-ed, describe style, not quality)

| Bucket | Rule (approx.) |
|---|---|
| `hft_mm` | ≥2,000 fills/day, or mostly maker with holds <5 min |
| `unprofiled` | <10 round trips |
| `scalper` | median hold <60 min |
| `dip_buyer` | enters after a down move (median side-signed 1h z ≤ −0.75), wins often (≥70%), small wins vs losses |
| `momentum_swing` | holds 4h–14d, enters with the 4h move (median z240 ≥ 0.5) |
| `swing_other` | holds ≥4h, other entries |
| `mixed` | everything else |

Tags: `illiquid`, `long_bias`, `short_bias`, `maker`, `fat_left_tail`.

## Datasets (release) — also the query views

| View | What |
|---|---|
| `universe` | every screened wallet: performance, style stats, bucket, tags, OOS since last vintage |
| `cohort_membership` | (as_of, address, rank_sharpe, rank_net, sharpe, net_mtm) per month-end vintage |
| `persistence` | per vintage × horizon (1/3/6 mo) × selector (all/top50_sharpe/top50_net) × bucket: n, pct_profitable, median_oos_sharpe |
| `fills` | raw fills, cohort only |
| `round_trips` | flat-to-flat trips: coin, t0, t1, side (1/−1), entry/exit px, peak size, notional, net, fee, ret_bp, hold_min, fills, maker_share, mae_bp, mfe_bp, ended_by_liq |
| `positions_hourly` | cohort hourly positions (ends the day before release) |
| `markouts` | per fill: edge vs prior minute, side-signed returns at 1m…1d (last 180 days) |
| `entry_states` | each trip's entry features (see below) + outcome |
| `leaderboard` | universe + 30-week sparkline + copy result at 5 min / 11 bp |
| `coin_flows` | daily net flow (USD, buys +) and long/short counts per coin × bucket |
| `entry_state_hist` | entry-feature histograms per bucket/side vs a baseline of every-3-hours points |
| `copyability` | per wallet × delay (1/5/15 min) × cost (5/11/20 bp): gross/net bp per trip, hit rate, out-of-sample net, verdict |
| `account_day`, `bars_1m`, `funding` | t0 aggregates (server queries only; not downloadable) |

## Entry features (`features_sql`)

At a moment (coin, ts): `z1, z5, z15, z60, z240, z1440` (return over k minutes divided by its typical size — side-agnostic; multiply by trip side for "with/against the move"), `dist_hi`, `dist_lo` (from 24h high/low), `vol_ratio`, `vol_z`, `btc_z60`, `btc_z240`, `hour_sin`, `hour_cos`, `weekend`, `funding`. The same SQL ships in every manifest so you can compute features for your own entries (`core.trader_lab.features.apply`).
