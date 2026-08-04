---
name: pattern-match
description: Use ONLY for a chart-selected Pattern Match delegated directly to wayfinder-quant. Starts with a cached exact-market baseline, then lets the quant agent add clearly labelled analogues when useful.
metadata:
  tags: pattern-match, technical-analysis, analogues, pattern-matching, chart
---

# Pattern Match

## Required first step

Call `wayfinder_quant_pattern_match` with the exact market, chart, interval,
timestamp, price-range, and contract/coin identifiers from Known Context. Do
this before research, scripts, or generic candle tools.

Treat the returned same-market sample as the baseline. Never fetch those exact
candles again.

## Widening the search

If same-market coverage is weak, there are fewer than 12 independent matches,
or the outcome distribution is unstable, decide whether another venue for the
same asset or a contextually relevant market would materially help. Select
those comparisons from the user's market and thesis; do not use a fixed peer
list merely to increase sample size.

For a liquid asset with a defensible CEX spot analogue, prefer
`wayfinder_quant_pattern_match_ccxt_proxy(match_id=..., symbol=...)`. It reuses
the cached selected pattern, fetches the same timeframe from CCXT, and returns
the proxy evidence separately. Do not use it for a long-tail token merely
because its ticker resembles a listed asset.

For other contextually relevant comparisons, use existing data tools or a
bounded script. The SDK's `PriceSeries` and `find_price_analogs` helpers are
available when deterministic comparison is useful. Reconstruct the selected
query from `pattern.shape_path_bps`; fetch only candidate histories, never the
exact market again. Keep the original baseline separate and tie all additional
analysis to completed candles at the analyzed interval.

Never silently blend fuzzy evidence into exact evidence. State:

- why you widened the search and why each comparison was selected;
- same-market, same-asset-proxy, and cross-market sample counts;
- source, interval, and lookback;
- how fuzzing changes confidence;
- any missing data or failed expansion.

Cross-market matches can add perspective but cannot upgrade a low-confidence
same-market result to high confidence. If no defensible comparison exists, say
the evidence is thin rather than forcing one.

## Analysis priorities

Lead with what the historical evidence actually supports:

1. Pattern direction, magnitude, range, and regime.
2. Same-market forward median, interquartile range, hit rate, and sample size.
3. Differences between exact and fuzzy samples when widening was used.
4. Concrete selection bounds, invalidation, and nearby levels.
5. The strongest counter-signal and the limits of the analogy.

Historical analogues are context, not a forecast. Avoid deterministic language
and never invent missing candles or outcomes.

## Utility views

Set `visualSpec` only when a view materially clarifies the result. Prefer one
focused line chart rather than several panes:

- selected normalized pattern against up to five analogue paths;
- forward-return medians and interquartile ranges by horizon;
- same-market versus fuzzy distributions when custom comparisons disagree.

Use the bounded `pattern.shape_path_bps` and match `shape_path_bps` values,
label units as basis points, and carry `market_id`, `chart_id`, and `match_id` in
`contextForNextAgent`. The primary agent will delegate the spec to
`wayfinder-visual`; do not call visual tools directly.

Return the standard quant JSON contract with a concise `analysisSummary`,
metrics, confidence, warnings, `contextForNextAgent`, and optional
`visualSpec`.
