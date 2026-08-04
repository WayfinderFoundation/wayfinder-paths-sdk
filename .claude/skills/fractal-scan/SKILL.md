---
name: fractal-scan
description: Legacy compatibility alias for Pattern Match. Use only when an older frontend requests the previous workflow name.
metadata:
  tags: compatibility, pattern-match, technical-analysis, chart
---

# Pattern Match compatibility

Follow the canonical `/pattern-match` workflow. Use
`wayfinder_quant_pattern_match` for the exact-market baseline and
`wayfinder_quant_pattern_match_ccxt_proxy` only when a defensible same-asset
CEX comparison would materially improve weak evidence.

The legacy `wayfinder_quant_fractal_scan` and
`wayfinder_quant_fractal_scan_ccxt_proxy` aliases remain available during the
rollout, but do not prefer them in new work.
