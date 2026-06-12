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
3. **De-vig correctness** — raw implied probabilities include vendor margin. High scores
   remove vig correctly for the market type (two-way, three-way 1X2 with the draw,
   whole-field normalization for futures) and say so. Comparing model/market numbers to
   RAW implied odds scores low.
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

Output STRICT JSON only:

```json
{
  "question": "<1-line restatement>",
  "scores": {
    "A": {"data_grounding": 0, "odds_sourcing": 0, "devig": 0, "posterior": 0,
           "adjudication": 0, "decision_quality": 0, "news_blend": 0, "total": 0},
    "B": {"data_grounding": 0, "odds_sourcing": 0, "devig": 0, "posterior": 0,
           "adjudication": 0, "decision_quality": 0, "news_blend": 0, "total": 0}
  },
  "verdict": "A|B|TIE",
  "margin": "decisive|clear|narrow",
  "rationale": "<=5 sentences citing concrete evidence from the texts",
  "best_of_loser": "<=2 sentences: what the losing answer did better, if anything"
}
```
