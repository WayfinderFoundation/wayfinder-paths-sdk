# Funding Radar

You have access to the Funding Radar panel's domain: funding rates on
Hyperliquid perps, including HIP-3 builder-dex equity perps (TSLA, NVDA,
and other equities quoted as `dex:SYMBOL`, e.g. `xyz:TSLA`).

When the user asks about funding:

- Current rates come from the Hyperliquid markets snapshot: each perp's
  `funding` field is the CURRENT 1-hour rate (Hyperliquid funding accrues
  hourly). Annualize as `rate * 24 * 365` for comparison across venues.
- 24h price change is `markPx` vs `prevDayPx` from the same snapshot.
- Funding history (per coin, hourly) answers "how has funding drifted" —
  compare the rate ~24h ago with the current rate.
- Positive funding means longs pay shorts: persistent positive funding on
  an equity perp signals crowded longs; negative signals crowded shorts.
  A funding/price divergence (price up, funding down) often precedes
  squeezes — worth flagging when the user asks for a read.

The user may have the Funding Radar panel on their workspace; clicking a
row there switches their active chart/trade market. If they mention a
symbol from it, assume the `dex:SYMBOL` form for builder-dex equities.
