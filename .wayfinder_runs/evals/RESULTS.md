# A/B eval results — sports method stack vs pre-sports baseline (2026-06-12)

Blind key: q1 A=baseline B=new · q2 A=new B=baseline. Judge: deepseek-v4-pro,
no tools, rubric scripts/eval_sports_ab_judge.md. Arms ran back-to-back per
question; baseline isolated (no sports tools/worker/skill; research→sports off).

| Question | Winner | Margin | New total | Baseline total |
|----------|--------|--------|-----------|----------------|
| q1 trophy-market scan | NEW | decisive | 60/70 | 31/70 |
| q2 Canada-Bosnia odds | NEW | clear | 48/70 | 37/70 |
| combined | NEW 2-0 | — | 108/140 | 68/140 |

Per-criterion (new vs baseline): q1 — devig 9v3, posterior 9v2, adjudication
9v5, decision 9v4, news 8v1, grounding 8v7, sourcing 8v9.
q2 — devig 7v3, posterior 7v6, adjudication 4v3, decision 6v5, news 8v6,
grounding 8v7, sourcing 8v7.

Judge highlights: new arm credited for explicit overround de-vig, the 7-card
Belgium evidence ledger with LLR magnitudes, EV-gated recommendations, and
coach-quote news integration. Baseline credited for breadth (48 countries x
PM+Hyperliquid) and liquidity caveats, but "compares raw implied probabilities
without de-vigging either market, no prior/posterior structure, directional
calls without gates, zero news" (q1).

Honest weaknesses surfaced: q2 new arm produced a de-vigged book column but
skipped the posterior CLI and made an ungated "3-5 points too rich" call
(judge: adjudication 4/10, "neither answer investigates why the divergence
exists"). The dislocation gate itself was correct (llr 0.068 < 0.08 -> no
adjudication required), but an ungated directional verdict on a sub-threshold
gap remains a discipline lapse to iterate on.

---

# Round 2 — overfit check (2026-06-12 evening, fresh questions, post-tightening)

Blind key: q1 A=new B=baseline · q2 A=baseline B=new · q3 A=new B=baseline.
Questions never tuned on: NBA Finals props + PM executability; F1 drivers futures
vs PM; "Should I bet the over in tomorrow's Yankees game?" (NBA game id overlaps
the original pipeline unit-verification game — incidental, only NBA game live.)
Baseline contamination: 0/3. New arms all engaged the stack (q1 via sports
delegation: prop_slate 114 rows; q2 futures_slate direct; q3 game_slate).

| Question | Winner | Margin | New | Baseline |
|----------|--------|--------|-----|----------|
| q1 NBA Finals props | BASELINE | decisive | 9/70 | 23/70 |
| q2 F1 drivers futures | NEW | decisive | 52/70 | 25/70 |
| q3 Yankees over | BASELINE | decisive | 18/70 | 35/70 |
| **Combined rounds 1+2** | **NEW 3–2** | — | 187/350 | 151/350 |

## What round 2 proved (the point of the exercise)

The method wins where its pipelines map cleanly onto the question (futures field +
dislocation gating: q2's VENUE NOISE labels and Russell WATCH-pending-adjudication
scored 9/10). It LOSES on:

1. **Composition** (q1): the sports worker ran prop_slate (114 rows) but the
   primary's final answer asserted "+7-10% EV, $200-700 sizing" WITHOUT showing the
   table — the judge scored ungrounded assertions 9/70 while the no-data baseline
   presented specific PM prop markets with samples and liquidity. Failure class:
   pipeline numbers lost between delegation and final answer.
2. **Model blind spot + venue follow-through** (q3): game_slate's 25-game form model
   omits STARTING PITCHERS (the dominant MLB totals driver — baseline cited
   Schlittler 1.87 ERA vs Gausman and concluded no edge); and the new arm ASKED
   "want me to check Polymarket?" while the baseline simply checked (O/U 7.5 at
   0.495/0.505, $1.5k liquidity). Same ask-instead-of-act gap in q2 (offered the
   Russell adjudication rather than running it).

## Fix list (implemented WITHOUT re-running these questions — no tuning on the test)

- Primary composition rule: paste the pipeline's top table rows into the final
  answer; numbers asserted must appear in a shown table/ledger.
- game_slate MLB: surface the un-modeled pitching matchup as a named limitation
  (and/or fetch probable pitchers as context) whenever sport=mlb.
- Autonomy: within a session, the agent completes the executable-venue check and
  the top adjudication itself — offering them as follow-ups is the q1/q3 failure
  mode by another name.

Judge fairness note: q3's "baseline properly concludes no edge" scores PROCESS, not
truth — the model's 59.9% over may yet be right; resolution tracking (the outcome
ledger next-step) is how we'd know.

Fix-list status (same evening): ALL THREE IMPLEMENTED — commit references the
round-2 losses. MLB pitcher layer live-verified on a different game (Kay x1.042
vs Sasaki x1.205; honest NOT-MODELED degradation when props absent). Eval
questions not re-run per the no-tuning rule; next fresh round will measure.

---

# Round 3 + 3b (2026-06-12 night) — post-fix generalization + broad-scan probes

Round 3 = fresh questions in round-2's classes (no question re-runs). Round 3b = the
user's broad-scan classes ("ALL the props in game X", "most mispriced WC markets across
everything"). Baseline contamination 0/5. Blind keys in r3_judge_key.txt.

| Question (class) | Winner | Score (new-baseline) | vs round 2 class result |
|---|---|---|---|
| r3q1 Dodgers props (composition class) | NEW, clear | 44-35 | FLIPPED (was 9-23) |
| r3q2 Braves over (pitcher/should-I-bet) | NEW, decisive | 60-21 | FLIPPED (was 18-35) |
| r3q3 US Open futures (control) | NEW, decisive | 54-2 | held (was 52-25) |
| r3bq1 ALL props, broad (props not posted) | baseline, clear | 34-43 | new class |
| r3bq2 WC cross-market broad scan | baseline, clear | 28-39 | new class |

Round 3+3b: NEW 3-2. Cumulative all rounds: NEW 6-4.

## Round-2 fixes: confirmed effective
All three round-2 loss classes flipped decisively. r3q2's answer is the template: full
5-vendor table with DK's 8.0-vs-8.5 line split flagged and excluded from consensus,
labeled Reference Model, loud UNKNOWN-starters warning, gated verdict. Composition rule
visible everywhere (3.2-8.7k answers with tables vs round-2's 1.1k prose).

## New failure class: BROAD scans (the user's instinct was right)
1. r3bq1 (props not posted): both arms honest, but baseline pivoted to the CONCRETE
   game-level board (PM 58.5c ML, HL prices, spread-vs-ML anomaly) while our arm
   narrated workflow (prep script, runner offer) with vaguely-attributed lines. Judge:
   "operationally more useful but doesn't satisfy the quantitative rubric."
   Lesson: when the asked market is empty, pivot to adjacent concrete markets with full
   sourcing — show the numbers you DO have.
2. r3bq2 (WC cross-market): our agent DID the full analysis (trophy across 3 venues
   with field-sum vig, groups gated through adjudication, honest unmapped corner) but
   the FINAL message carried only the tail of it — the judge saw a 2k tail vs the
   baseline's single 5.3k comprehensive answer. Plus a fair critique: our "venue noise"
   labels replaced structural explanation where the baseline investigated WHY venues
   diverge (daily-expiring vs perpetual contracts). Lessons: (a) final-message assembly
   — long multi-stage answers must be assembled IN FULL in the last message; (b) the
   noise label is for sub-threshold gaps, not a substitute for naming structural
   venue differences.

Also notable: baseline collapsed to 2/70 on golf futures (no data source = nothing to
say) — the widest gap yet; and judge fairness held (it credited the baseline's
operational ideas and dinged our news_blend where absent).

## User-caught shared blind spot (post round-3b)
The user pointed at polymarket.com/sports/mlb/mlb-lad-cws-2026-06-12: a per-game PM
EVENT with 26 markets (alt spreads ±1.5..±4.5, alt totals 5.5-11.5, F5 lines, game
props) that BOTH arms ignored while the question asked "is any of it executable on
Polymarket". Meta-lesson: blind A/B judging cannot catch what both arms miss —
ground-truth spot checks matter. Fixes (implemented, not re-scored): executable-board
rule (hydrate the game event slug, enumerate the board) + game_slate alt_lines ladder
(the grid prices every alt line for free) + explicit date-resolution rule.

---

# Round 4 + grounded-judge deployment (2026-06-12 late night)

## Grounded judge re-scores round 3b: BOTH blind verdicts reversed

| Pair | Blind (text-only) | Grounded (PM+HL+snapshot research first) |
|------|-------------------|------------------------------------------|
| 3b ALL-props | baseline 43-34 | **NEW 49-32** — baseline analyzed the WRONG GAME (June 12, not tomorrow's) and quoted PM prices "matching neither observed board — cannot be attributed to drift" |
| 3b WC cross-market | baseline 39-28 | **NEW 44-31** — baseline FABRICATED HL match probabilities (quoted 48/29/23 vs live 94/4.6/3.7) and its praised "daily-expiring contracts" insight is FALSE (they're season-long) |

Meta-lesson confirmed: the blind judge systematically rewarded confident,
specific-sounding fabrication ("precise executable prices", "structural analysis").
Grounding the judge in live market reads is what catches it. Corrected cumulative
through round 3b: NEW ~8-2.

## Round 4 (fresh: "whole board for tomorrow's Astros at Royals"): NEW LOSS 22-31

A NEW failure mode, symmetric justice: the executable-first funnel mechanics all ran
(board enumerated with liquidity, gating language, HL tools' first outing) but the
agent CONFLATED TWO GAMES — the provider's UTC date filter returns both the
in-progress June-12-evening game and the scheduled June 13 game under one date (US
evening games cross the UTC line; the eval author's prep made the same mistake). It
compared the live game's in-game book odds (total 20.5, HOU -800) against the
scheduled game's pre-game PM board (9.5, 46.5c) and manufactured a fictional
"Polymarket clearBookOnStart failed / all 26 markets stale / nothing tradeable"
narrative. The grounded judge caught our fabrication exactly as it caught the
baseline's. (Baseline also dinged: never cited the 6 book vendors, same two-game
blindness, but its answer matched the correct game's observed prices.)

Fix shipped (not re-scored): UTC-boundary disambiguation rule in the primary — list
all same-matchup games across adjacent dates with datetimes/statuses before analyzing;
never mix one game's live odds with another's pre-game board.

Cumulative honest tally: NEW 8-3 with grounded judging where available.

---

# Uniform grounded GPT-5.5 re-judge (2026-06-13) — one judge, all 6 recent pairs

Identical DeepSeek arms throughout; a single independent judge (openai/gpt-5.5, high
reasoning, research-first grounding) re-scored every archived pair. Replaces the earlier
mix of blind-DeepSeek (rounds 1-2) and grounded-DeepSeek (3b, 4) verdicts.

| Question (class) | Grounded GPT-5.5 | new | base | vs blind judge |
|------------------|------------------|-----|------|----------------|
| Dodgers props (composition) | NEW clear | 27 | 14 | held |
| Braves over (pitcher) | NEW decisive | 60 | 19 | held |
| US Open futures (control) | NEW decisive | 56 | 4 | held |
| ALL props broad | **NEW decisive** | 40 | 20 | **FLIP** (blind said baseline) |
| WC cross-market broad | **NEW clear** | 46 | 33 | **FLIP** (blind said baseline) |
| Astros board (UTC two-game trap) | baseline clear | 20 | 27 | held |

**Grounded GPT-5.5: NEW 5-1.** Both broad-scan "losses" the blind judge had awarded to
the baseline FLIP to NEW once the judge checks live reality — the blind judge had been
rewarding the baseline's fabrications (wrong-game analysis + invented prices on ALL-props;
fake HL match probabilities + a false "daily-expiring contracts" claim on WC cross-market).
The one honest NEW loss survives grounding: round-4 Astros, our own UTC two-game conflation
(fix shipped: the disambiguation rule). Cross-model robustness also confirmed — GPT-5.5 and
DeepSeek-grounded agree on direction for every pair they both scored.

Meta-conclusion: blind LLM judging systematically over-rewards confident, specific-sounding
fabrication; grounding the judge in executable-market reads is what corrects it. The eval
signal is now both model-robust (two judge models agree) and ground-truth-anchored.
