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

For a liquid asset with a defensible perpetual analogue, prefer
`wayfinder_quant_pattern_match_ccxt_proxy(match_id=..., symbol=...)`. It reuses
the cached selected pattern, tries the supported linear-perp venues in order,
and returns the proxy evidence separately. It never substitutes spot. Do not
use it for a long-tail token merely because its ticker resembles a listed
asset.

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
2. Shape, magnitude, and realized-volatility similarity components for the
   strongest matches, including their dates.
3. Same-market forward median, mean, interquartile range, hit rate, and sample
   size at every available wall-clock horizon.
4. Differences between exact and fuzzy samples when widening was used,
   including the selected perp venue.
5. Concrete selection bounds, invalidation, nearby levels, and the strongest
   counter-signal.
6. The analyzed interval, omitted sub-interval horizons, coverage, and limits
   of the analogy.

Historical analogues are context, not a forecast. Avoid deterministic language
and never invent missing candles or outcomes.

## Utility views

Pattern Match tool results include a bounded `visual_spec`. Return the most
complete one unchanged as `visualSpec` (the proxy result supersedes the exact
one when present). It targets the existing chart with an outcome fan and top
analogues; do not replace it with a newly generated chart spec.

Carry `market_id`, `chart_id`, `match_id`, analyzed interval, selected perp
venue, and sample counts in `contextForNextAgent`. The primary agent will
delegate the spec to `wayfinder-visual`; do not call visual tools directly.

Return the standard quant JSON contract with a concise `analysisSummary`,
metrics, confidence, warnings, `contextForNextAgent`, and optional
`visualSpec`.
