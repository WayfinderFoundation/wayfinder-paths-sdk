# Blind judge rubric — market and sports edge answer quality

You are judging two anonymous answers (A and B) to the same market or sports-edge question,
produced by two different agent configurations at roughly the same time. You do NOT know
which configuration produced which answer. Questions may cover prediction markets, liquid
perps/spot assets, broad sports outrights, or specific game lines.

Grounded judge mode: use only the read-only validation tools allowed by the
`wayfinder-eval-judge` agent for a bounded grounding pass before scoring. Use that pass
to check board coverage, current state, structural price/venue reality, and obvious
news/data context. Do not run a full competing model and do not create your own trade or
betting thesis. Score answer quality from the answer texts plus your bounded observations.

Score each answer 0–10 on every criterion:

1. **Data grounding** — are the numbers specific, internally consistent, and plausibly
   from real feeds (named venues, line values, prices, order books, liquidity, dates)?
   Vague or invented-looking numbers score low.
2. **Source discipline** — executable venues and first-party data feeds score high:
   PM/HL order books for prediction markets, Hyperliquid/venue data for perps/spot,
   sports provider data for games, and credible primary/current sources for qualitative
   evidence. Media/web odds, stale articles, or unattributed numbers score low.
3. **Executable market math** — PM/HL order-book prices are the executable surface for
   prediction-market bets; live venue price/depth/funding/borrow availability is the
   executable surface for tradable assets. High scores use bid/ask/mid/depth correctly,
   preserve multi-outcome mappings (e.g. home/draw/away or field outrights), normalize
   complete outcome sets where needed, and avoid treating last trade, media odds, or
   optional context as executable. Compact board-first surfaces plus resolver/profile
   references are preferred over giant raw payloads; full payout matrices only matter for
   shortlisted or non-standard markets. If sportsbook context is used, de-vigging it
   correctly is useful but not required.
4. **Prior & posterior discipline** — is there a clearly named prior (ideally the
   executable market price)? Is evidence folded in transparently (itemized, with
   magnitudes), with double-counting avoided (news that predates the posted lines is
   already in them)? Freehand probability adjustments with no ledger score low.
5. **Disagreement adjudication** — when two venues disagree, does the answer investigate
   WHY the cheap side is cheap (structural: resolution rules, lockup, flow; or
   informational) before recommending it? Trusting one venue blindly scores low.
6. **Decision quality & calibration** — are recommendations gated (EV thresholds,
   conservative bands, WATCH/SKIP states), sized, and liquidity/risk-aware? Is "no edge"
   stated when the evidence supports no edge? For prediction markets, does the answer say
   whether the edge is settlement, exit-before-close/mark-to-market, relative value, or
   arb/conversion? Confident calls without gates or an exit/settlement plan score low.
7. **News/data blend** — is current news (injuries, lineups) integrated with the
   quantitative view in a disciplined way (what's priced in vs what isn't), rather than
   bolted on or ignored? For non-sports questions, this includes company/private-market
   facts, token/funding/liquidity context, ETF/stock structure, catalysts, borrow/funding,
   and clearly dated sources.
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
   Stopping at PM-vs-HL or optional book-vs-market spread comparison scores low. So does
   presenting a single latest simulator output as final fair value without distilling it
   against executable market priors, model provenance, current-state evidence, and
   diagnostic flags such as approximate bracket or market-implied ratings.

Question-specific grading notes:

- **IPO prediction markets** — high scores hydrate the relevant PM/HL board, inspect
  resolution text (what counts as IPO/first, SPAC/direct listing ambiguity, deadlines,
  "neither/other" handling), compare all executable outcomes, and research current
  company timing evidence without over-weighting hype. Penalize answers that answer from
  vibes or one article without board math, or that use binary EV on partial 50/50,
  multi-outcome, neg-risk, or custom-resolution profiles. A compact board plus clear
  resolution profile is enough; do not require a full payout matrix inline, a bespoke
  script, or a backtest for a simple one-market edge check. Penalize progress-checkpoint
  answers, "continue the analysis" handoffs, or answers that defer a clear `BUY`/`WATCH`/
  `SKIP`/`NEEDS_REPAIR` conclusion solely because more internal modelling could be done.
  Do not penalize a trailing `<userSuggestions>` block when it appears after a complete,
  authoritative answer; it is back matter, not part of the scored analysis.
- **HYPE/SPCX short setup** — high scores identify the exact instruments (HYPE perp/spot
  vs any similarly named ticker; SPCX equity/ETF/venue availability), pull current price
  context where available, check whether a short is actually executable, and define a
  risk plan with invalidation, stop, target, sizing/risk budget, and entry conditions.
  Penalize naked short recommendations with no borrow/funding/liquidity/volatility check,
  or confusing HYPE with unrelated "hype" narratives.
- **World Cup countries/outrights** — high scores hydrate the country-winner board across
  PM/HL, condition on current tournament state, classify stale/live/clean signals, and use
  a path-aware model or explicit path assumptions. Penalize stopping at book-vs-market or
  PM-vs-HL spread comparisons.
- **Specific MLB game lines** — high scores resolve the exact Rays/Nationals game date,
  starter/injury/weather context where available, executable PM/HL board coverage if it
  exists, and fair moneyline/game-line estimates with clear uncertainty. Penalize failure
  to handle "tomorrow" as a concrete date, missing the actual board, or presenting model
  estimates as executable prices.
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
