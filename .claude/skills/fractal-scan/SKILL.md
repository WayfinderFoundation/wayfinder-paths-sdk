---
name: fractal-scan
description: Use ONLY for a chart-selected Fractal Scan delegated directly to wayfinder-quant. Starts with exact same-market analogues, widens transparently when evidence is weak, and returns bounded technical views.
metadata:
  tags: fractal-scan, technical-analysis, analogues, pattern-matching, chart
---

# Fractal Scan

## Required first step

Call `wayfinder_quant_fractal_scan` with `scope="same_market"` and the exact
market, chart, interval, timestamp, price-range, and contract/coin identifiers
from Known Context. Do this before research, scripts, or generic candle tools.

Treat the returned same-market sample as the baseline. Never fetch those exact
candles again.

## Widening the search

If same-market coverage is weak, there are fewer than 12 independent matches,
or the outcome distribution is unstable, call the same tool again with its
`scan_id` and `scope="adaptive"`. Use `scope="broad"` only when a same-asset
venue proxy or clearly labelled cross-market analogue would materially help.

You may use other quant tools or a bounded script after the baseline when the
prepared perspectives do not answer an important question. Reuse the scan data
and returned view data; do not rediscover the market or repeat the baseline
pull. Keep all additional analysis tied to the selected pattern and completed
candles.

Never silently blend fuzzy evidence into exact evidence. State:

- why the scan widened;
- same-market, same-asset-proxy, and cross-market sample counts;
- source, interval, and lookback;
- how fuzzing changes confidence;
- any missing data or failed expansion.

Cross-market matches can add perspective but cannot upgrade a low-confidence
same-market result to high confidence.

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
- median forward path with 25th–75th percentile bounds;
- same-market versus fuzzy forward paths when they disagree.

Use bounded inline points from `view_data`, label units as basis points, and
carry `market_id`, `chart_id`, and `scan_id` in `contextForNextAgent`. The
primary agent will delegate the spec to `wayfinder-visual`; do not call visual
tools directly.

Return the standard quant JSON contract with a concise `analysisSummary`,
metrics, confidence, warnings, `contextForNextAgent`, and optional
`visualSpec`.
