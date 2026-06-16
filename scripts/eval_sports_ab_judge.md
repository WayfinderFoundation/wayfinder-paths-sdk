# Blind judge rubric — sports betting answer quality

You are judging two anonymous answers (A and B) to the same sports-betting question,
produced by two different agent configurations at roughly the same time. You do NOT know
which configuration produced which answer. Judge ONLY from the answer texts — do not use
tools, do not fetch anything, do not re-derive market prices.

Score each answer 0–10 on every criterion:

1. **Data grounding** — are the numbers specific, internally consistent, and plausibly
   from real feeds (named books/venues, line values, liquidity)? Vague or invented-looking
   numbers score low.
2. **Odds sourcing discipline** — betting lines from a first-party data feed and/or the
   executable venue's order book score high; odds attributed to media/web pages
   (FOX/ESPN/RotoWire etc.) or unattributed score low.
3. **Executable market math** — PM/HL order-book prices are the executable surface.
   High scores use bid/ask/mid/depth correctly, preserve multi-outcome mappings (e.g.
   home/draw/away), normalize complete HL/PM outcome sets where needed, and avoid treating
   last trade, media odds, or optional sportsbook context as executable. If sportsbook context is used,
   de-vigging it correctly is useful but not required.
4. **Prior & posterior discipline** — is there a clearly named prior (ideally the
   executable market price)? Is evidence folded in transparently (itemized, with
   magnitudes), with double-counting avoided (news that predates the posted lines is
   already in them)? Freehand probability adjustments with no ledger score low.
5. **Disagreement adjudication** — when two venues disagree, does the answer investigate
   WHY the cheap side is cheap (structural: resolution rules, lockup, flow; or
   informational) before recommending it? Trusting one venue blindly scores low.
6. **Decision quality & calibration** — are recommendations gated (EV thresholds,
   conservative bands, WATCH/SKIP states), sized, and liquidity-aware? Is "no edge"
   stated when the evidence supports no edge? Confident calls without gates score low.
7. **News/data blend** — is current news (injuries, lineups) integrated with the
   quantitative view in a disciplined way (what's priced in vs what isn't), rather than
   bolted on or ignored?
8. **Ground-truth coverage** (grounded judge only; text-only judges score it 5 for both) —
   against the markets YOU observed live: did the answer engage the board that actually
   exists (or honestly scope what it skipped), and do its quoted markets/venues/prices
   correspond to reality? Structural misses (existing markets ignored while claiming
   completeness or "nothing executable", invented-looking quotes, wrong venue) score low;
   small price drift since the answer was written must NOT be penalized.
9. **Current-state conditioning** — for live/path-dependent sports events, does the answer
   condition on completed games/matches, standings, injuries/availability, and timestamps?
   Answers that compare pre-event model numbers to post-result markets without labeling the
   mismatch score low.
10. **Path/simulation depth** — for outrights, brackets, group winners, season awards, or
   any field market where path matters, does the answer go beyond cross-venue price gaps
   into a transparent path model, bracket/state simulation, or explicit path assumptions?
   Stopping at PM-vs-HL or optional book-vs-market spread comparison scores low.

Question-specific grading notes:

- **Unsupported sports/data trick questions** — high scores require probing the supported
  sports/provider and executable-market surfaces, then reporting unavailable coverage
  cleanly. Penalize invented fight odds, invented stats, made-up market availability, or
  recommendations when the provider/executable market is unsupported or missing.
- **Estimated spreads/totals** — credit creative non-odds data only when it is clearly
  separated from executable PM/HL lines and provider betting context. Penalize web/media odds sourcing, unlabeled
  estimates presented as book lines, or point/goal totals that are not sport-normalized.

Output STRICT JSON only:

```json
{
  "question": "<1-line restatement>",
  "ground_truth": {
    "observedAt": "<ISO timestamp or null for text-only judging>",
    "markets_observed": {"polymarket": 0, "hyperliquid": 0, "sportsbook_context_optional": 0},
    "missed_by_A": ["<existing market/board area answer A ignored>"],
    "missed_by_B": [],
    "price_flags": ["<structural price/venue problems, attributed to A or B>"],
    "notes": ""
  },
  "scores": {
    "A": {"data_grounding": 0, "odds_sourcing": 0, "executable_market_math": 0, "posterior": 0,
           "adjudication": 0, "decision_quality": 0, "news_blend": 0,
           "ground_truth_coverage": 0, "current_state_conditioning": 0,
           "path_simulation_depth": 0, "total": 0},
    "B": {"data_grounding": 0, "odds_sourcing": 0, "executable_market_math": 0, "posterior": 0,
           "adjudication": 0, "decision_quality": 0, "news_blend": 0,
           "ground_truth_coverage": 0, "current_state_conditioning": 0,
           "path_simulation_depth": 0, "total": 0}
  },
  "verdict": "A|B|TIE",
  "margin": "decisive|clear|narrow",
  "rationale": "<=5 sentences citing concrete evidence from the texts and your observations",
  "best_of_loser": "<=2 sentences: what the losing answer did better, if anything"
}
```

Totals are out of 100 (10 criteria x 10).
