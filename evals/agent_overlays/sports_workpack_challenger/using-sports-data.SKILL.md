---
name: using-sports-data
description: The Wayfinder sports data catalog — every canonical resource, its params, and which sports support it; betting (odds/props/futures) coverage per league; Lab (backtesting) specifics; id/season/category conventions.
metadata:
  tags: wayfinder, sports, betting, props, odds, backtesting, lab
---

## What you need to know (TL;DR)

All sports data flows through three MCP tools backed by the backend gateway (the provider
key never leaves the backend; the surface is provider-agnostic):

- `wayfinder_sports_snapshot` — bounded live reads (scoreboard/odds/props/injuries/lookups/stats).
- `wayfinder_sports_provider` — the full façade: `action="catalog"` lists every callable
  `endpoint_id` **with `supported_leagues` per data endpoint — that catalog is the runtime
  source of truth for "which sports support what"**; `action="call"` invokes one.
- `wayfinder_sports_backtest_state` — canonical run/job monitoring for Lab backtests.

An unsupported (resource, sport) combo returns `resource_unavailable_for_league` **with the
leagues that DO support it** — never guess availability, read the error or the catalog.

Leagues: `nba nfl mlb nhl wnba ncaaf ncaab ncaaw cbb epl laliga seriea bundesliga ligue1 ucl
mls worldcup mma f1 atp wta pga cs2 lol dota2`. Lab (backtesting) = **nba/nfl/nhl/mlb only**.
Common wrong guesses → right slug: `fifa`/`fiba` → `worldcup`; `soccer` → a league slug
(`epl`/`laliga`/`ucl`/...); `football` → `nfl`; `tennis` → `atp`/`wta`; `golf` → `pga`.
An invalid slug returns a 400 listing the valid choices — read it, don't keep guessing.

## Canonical resources (what each returns, key params)

Resource ids are generic and resolve per-league (e.g. `competitors` = players/fighters/drivers;
`events` = games/matches/MMA events/F1 sessions/golf tournaments). Call shape:
`sports_provider(action="call", endpoint_id="data.<resource>.<list|get>", sport=..., query={...}, path_params={...})`.

| endpoint_id | Returns | Key params |
|---|---|---|
| `data.events.list` / `data.event.get` | schedule/fixtures + results | `query`: `dates[]`, `seasons[]`, `per_page`; get: `path_params.id` |
| `data.teams.list` / `data.team.get` | teams / clubs / constructors | `query.per_page` |
| `data.competitors.list` / `.get` | players / fighters / drivers | `query`: `search`, `player_ids` (array), `per_page` |
| `data.competitors_active.list` | current rosters only | as above |
| `data.standings.list` | standings / rankings / driver standings | `query.season` (most team sports require it) |
| `data.team_standings.list` | constructor/team standings (F1) | `query.season` |
| `data.player_stats.list` | per-game logs | `query`: `player_ids` (array), `seasons[]`, `dates[]`, `per_page`, `postseason` |
| `data.player_season_stats.list` | player season totals | `query`: `season`, `player_ids` where supported; NHL is per-player: `path_params.player_id` |
| `data.season_averages.list` | per-player season averages (NBA-family) | `query`: `season`, `player_id`; `path_params.category` + `query.type` for categorized |
| `data.team_season_averages.list` | team season averages | `query`: `season`, `season_type` (`regular`/`playoffs`), `type` (`base`/`advanced`); `path_params.category` |
| `data.team_stats.list` | team per-game stats | `query`: `game_id`/`season` |
| `data.team_season_stats.list` | team season stats | `query.season`; NHL per-team: `path_params.team_id` |
| `data.player_advanced_stats.list` | advanced metrics | `query`: `seasons[]`, `player_ids`; NFL: `path_params.category` = `rushing`/`passing`/`receiving` |
| `data.leaders.list` | stat leaders | `query`: `season`, `stat_type` |
| `data.injuries.list` | injury/availability report | `query.per_page` |
| `data.box.list` / `data.box.live` | box scores (historical / live) | `query.date` / none |
| `data.lineups.list`, `data.plays.list` | lineups, play-by-play | `query.game_id` (game-scoped) |
| `data.matchups.list` | tennis head-to-head; MLB batter-vs-pitcher | tennis: `query` player ids; MLB versus: batter/pitcher ids |
| `data.career_stats.list` | career stats (tennis) | `query.player_id` |
| `data.shots.list` | soccer shot maps with **xG** | `query.match_id`/`game_id` depending on league |
| `data.match_events.list` | soccer goals/cards/subs | `query.match_id`/`game_id` depending on league |
| `data.momentum.list`, `data.pregame_forms.list` | soccer momentum / recent form | `query.match_id`/`game_id` depending on league |
| `data.rosters.list` | rosters; NFL depth charts | soccer: `query`; NFL: `path_params.team_id` |
| `data.results.list` | F1 session results / PGA tournament results / MMA fight results | `query`: season/event/fight/tournament filters |
| `data.qualifying.list`, `data.pit_stops.list`, `data.laps.list` | F1 detail (plan-gated upstream) | `query` session/event ids |
| `data.venues.list` | circuits / stadiums / courses | — |
| `data.round_stats.list` | PGA strokes-gained round stats | `query` tournament/player |
| `data.splits.list`, `data.plate_appearances.list` | MLB splits / plate appearances | `query` player/season |
| `data.pitcher_pitch_stats.list`, `data.hitter_pitch_stats.list` | MLB pitch-type breakdowns | `query` player/season |
| `data.conferences.list`, `data.bracket.list` | college conferences / March Madness bracket | `query.season` |
| `data.player_contracts.list`, `data.team_contracts.list` | NBA salaries/payroll (plan-gated upstream) | `query` |
| `data.shot_locations.list` | WNBA shooting zones | `query` player/season |
| `data.odds.list` | odds context | snapshot: `event_id`/`game_id`/`fight_id`/`tournament_id` OR `date`; façade: provider-specific ids |
| `data.player_props.list` | player prop lines + over/under odds | snapshot: `event_id`; façade: `game_id`, `match_id`, or `tournament_id` |
| `data.futures.list` | outright/futures odds | snapshot: `season`, optional `event_id`/`market_type`; façade: provider-specific filters |

## Which sports have what (highlights — catalog is authoritative)

- **nba** — richest: full stats family (game logs, season averages + categories
  `general/clutch/shooting/playtype/tracking/hustle/defense/shotdashboard` with `type` sub-param,
  team averages incl. `pace`/`def_rating` under `type=advanced`), advanced stats, box/live box,
  lineups, plays, leaders, injuries, contracts (plan-gated), odds + player props (the only league
  on the absolute v2 betting surface — handled transparently).
- **nfl** — game logs, season stats, team stats + team season stats, advanced
  rushing/passing/receiving (via `category`), per-team depth-chart rosters
  (`path_params.team_id`), plays, injuries, standings, odds + props.
- **mlb** — game logs, season stats, **batter-vs-pitcher matchups**, splits, plate appearances,
  pitch-type stats (pitcher + hitter), lineups, plays, injuries, odds + props.
- **nhl** — box scores, plays, injuries, standings; season stats are **per-player/per-team
  id-scoped** (no flat game-log endpoint); player/team leaders; odds + props.
- **wnba** — NBA-style stats + advanced + **shot_locations**; odds + props.
- **soccer** (epl/laliga/seriea/bundesliga/ligue1/ucl/mls/worldcup) — matches, rosters, injuries,
  standings, player/team match stats, **xG shots**, match events, momentum, pregame forms;
  odds + props; **futures** for ucl/worldcup. EPL serves from its v2 API transparently.
- **tennis** (atp/wta) — players, matches, rankings (as `standings`), **head-to-head matchups**,
  match stats, career stats; odds only.
- **mma** — fighters, events (cards), **fight results** (`data.results.list`), fight stats,
  rankings; odds only.
- **f1** — drivers, constructors (`teams`), sessions (`events`), qualifying, results, laps,
  pit stops, driver + team standings, venues; **futures only** (no per-race odds). Much of the
  deep telemetry is plan-gated upstream — expect `resource_unavailable_for_league` if unentitled.
- **pga** — players, tournaments, results, strokes-gained round stats, venues (courses);
  **futures + props**, no match odds.
- **college** (ncaaf/ncaab/ncaaw/cbb) — teams, players, games, standings, plays, conferences,
  **bracket** (ncaab/ncaaw March Madness); odds only, no props.
- **esports** (cs2/lol/dota2) — teams/players/matches (cs2 deepest: match/map stats); **no betting**.

## Betting coverage map

| Markets | Leagues |
|---|---|
| odds + player props | nba, nfl, mlb, nhl, wnba, epl, laliga, seriea, bundesliga, ligue1, mls, ucl, worldcup |
| odds only | ncaaf, ncaab, ncaaw, cbb, mma, atp, wta |
| futures | ucl, worldcup, f1, pga |
| none | cs2, lol, dota2 |

Sportsbook odds/props/futures are **optional context, never the executable price**.
Wayfinder sports views are actionable only against Polymarket or Hyperliquid prediction
market books. Compute edges with `wayfinder_paths.quant.sports_props.market_edge(...)`
or the prediction-market quant helpers against PM/HL order-book prices.

## Conventions that bite

- **Arrays**: list-valued query params just work — `query={"player_ids": [161, 1057262518]}`
  bulk-fetches in one call (the gateway maps to the provider's `key[]` form). Batch a whole
  slate's game logs in ONE `data.player_stats.list` call.
- **Ids are one namespace per sport**: the `player_id` in props, game logs, and players are the
  same id space (newer players just have huge ids). Hydrate names via `data.competitors.list`
  with `player_ids`.
- **Seasons**: integer start-year (`2025` = the 2025-26 NBA season). `season_type`:
  `regular`/`playoffs` where supported; game logs accept `postseason` true/false.
- **Id-scoped resources**: where the error or this doc says per-team/per-player, pass
  `path_params={"team_id": ...}` or `{"player_id": ...}` (NFL rosters, NHL season stats).
- **Snapshot id convention**: prefer `event_id` from scoreboard cards. The backend maps it to
  `game_id` for US team sports, `match_id` for soccer/World Cup props, `fight_id`/`event_id`
  for MMA odds, `tournament_id` for PGA props and tennis tournament odds, and `event_ids[]`
  for F1 futures. `game_id` remains a legacy alias.
- **Provider façade id convention**: use the exact provider filter requested by the endpoint
  (`game_id`, `match_id`, `fight_id`, `tournament_id`, `event_ids[]`, etc.); call catalog or
  read errors instead of guessing.
- **Caching**: non-live data (stats/averages/rosters) is cached server-side for hours — repeats
  are cheap; odds/props/futures stay near-live (~15s). Still batch.
- **Pagination**: `per_page` (max 100) + cursor in `meta.next_cursor` where present.

## Scripted analysis (inside `core_run_script`)

**Betting analysis is a FUNNEL that starts from the executable boards: (1) ENUMERATE
what's tradeable — the Polymarket per-game event (`get_event` on the
`{league}-{away}-{home}-{date}` slug) and Hyperliquid HIP-4 outcomes — into a candidate
table; (2) INFORMATION via sports data and the canned pipelines below where useful
(complete fetches, model features, optional sportsbook-context market math; never pull
odds from the web); (3) TRIAGE candidates by liquidity and gap; (4) DEEP-DIVE
survivors with whatever data sharpens the number (full matchup history across seasons,
comparable players vs that opponent, minutes/usage given injuries/lineups — multi-season
`player_stats`, `matchups`, advanced stats), each input a weighted evidence card; (5)
GATE per candidate and answer with the annotated board. MODELING is the agent's
judgment: `game_slate` separates an INFORMATION section (facts) from a labeled
REFERENCE MODEL (one opinion — adjust or replace it; `--data-only` for facts alone),
and your own view is expressed as evidence cards gated through `sports_posterior` over
the executable prior:**

**Broad prop/crossbet scans are sports-first, with novelty second.** For broad "any
props worth taking/selling" requests, first run cheap category discovery across real
sports markets: match outcomes/game lines, visible player/team stat props, goals,
points, totals/bands, exact score, more-markets, and specials. Treat announcer-word,
broadcast, entertainment, and other bespoke PM/HL props as a secondary novelty bucket,
not the default center of the answer. Do not stop at the first category that returns
results. Hydrate the discovered event ladders, inspect resolution text, and gate by
spread/liquidity. It is valid to return `BUY (heuristic)` / `SELL (heuristic)` when
the relative-pricing gap is obvious, but do not center word/phrase markets unless the
user explicitly asked for broadcast props or non-word categories were scanned and did
not surface useful executable markets. Include categories scanned/found/not found,
label confidence, and avoid unsupported true-prob claims.

```
# player props -> ACTIONABLE/WATCH/EXCLUDED EV table
poetry run python -m wayfinder_paths.quant.prop_slate \
  --sport nba --game-id <GAME_ID> --season <SEASON> --out .wayfinder_runs/sports

# game markets (moneyline/total/spread) -> data/model context and optional book context
# (PM/HL order books remain the executable price)
poetry run python -m wayfinder_paths.quant.game_slate \
  --sport nhl --game-id <GAME_ID> --season <SEASON> --date <GAME_DATE> --out .wayfinder_runs/sports
```

```
# futures fields (tournament winner / group winner / reach-final) -> optional book-context fair_p
poetry run python -m wayfinder_paths.quant.futures_slate \
  --sport worldcup --market-type outright --out .wayfinder_runs/sports

# context/model fair vs executable PM/HL disagree? -> dislocation check + gated posterior ledger
poetry run python -m wayfinder_paths.quant.sports_posterior \
  --market <PM_PRICE> --book <FAIR_P> --vendors <N> --overround <O> \
  [--card "davies_out:against:medium:news"]
```

**Composition and autonomy rules:** Every probability, edge, or EV asserted in the final
answer must appear in a shown table or posterior ledger. If the sports worker returns a
rendered table, paste the top rows; if it returns only `dataFiles`, read the artifact and
show the rows. Finish the executable-venue check and top dislocation adjudication
in-session instead of offering them as follow-ups.

**Executable board rule:** Polymarket lists a per-game EVENT (slug
`{league}-{away}-{home}-{YYYY-MM-DD}`, e.g. `mlb-lad-cws-2026-06-12`) carrying a whole
board — alternate spreads/totals, first-half/F5 lines, game props. Hydrate it
(`polymarket_read get_event`) and enumerate its markets as the executable candidate
set; `game_slate` emits `alt_lines` (model probabilities for the alt ladder) to price
them. "No provider props" never means "nothing executable."
For multi-outcome match boards (soccer/worldcup, tennis sets, MMA method, etc.), preserve
the returned outcome mapping exactly. A three-way soccer board is home/draw/away; never
collapse it into a binary or recommend "buy No on the favorite" unless a binary No token
is explicitly listed. For Hyperliquid HIP-4, only request mids for `#...` assets surfaced
by the market search or prior metadata; search with plain text `query` + `limit` only,
do not pass extra filters such as `market_type`, and do not derive sibling asset ids by
changing the last digit.

**Exact-market hydration rule:** For a named match/fight/event, never conclude "no
Polymarket market" from a search summary alone. If `polymarket_read(action="search")`
returns any plausible `eventSlug`, candidate, or event group for the names/date/card,
immediately hydrate it with `get_event` and inspect the candidate board. If exact-name
search is empty, run bounded fallback queries for **each competitor/team surname or short
name independently** plus the sport/card/date, then hydrate every same-card/same-date
event returned before declaring absence. Broad searches such as "ufc fight" are
truncation-prone discovery only; they are not negative proof unless every plausible event
candidate they surface has been hydrated with `get_event`. Only after those hydrated
checks can you say no executable PM market was found; phrase it as "not surfaced by these
searches" when coverage is incomplete.
Likewise, for a named game/fight/event, still search the direct matchup on PM and
Hyperliquid HIP-4 before calling the analysis complete, even when provider odds are
unavailable.

**UTC-boundary trap:** US evening games can cross the UTC date line. For scoreboard reads,
pass the user's IANA `timezone` with the concrete `date` and inspect the returned
`dateContext` before answering. Before analysis, identify the concrete game id, local/UTC
start, and status; NEVER mix one game's live book odds with another game's pre-game venue
board.

**Odds sourcing rule:** never source betting lines from web search or media pages.
Executable lines come from PM/HL order books. Provider sportsbook odds are optional
context only; if they are empty, unmatched, or auth-blocked, continue with PM/HL boards,
sports data, and model estimates rather than checkpointing. Web research is for news and
qualitative evidence, not odds.

**Dislocation rule:** when an optional sportsbook/context number or model estimate and
the PM/HL executable price for the same outcome disagree enough that
`sports_posterior.dislocation` flags it, never recommend the cheap side on trust — the
prior is the EXECUTABLE price, the context/model number enters as one capped evidence card,
and the gap must be adjudicated (research: "what explains the cheap side?" —
post-line news, resolution-rules mismatch, lockup/flow, stale data, method risk) before
any recommendation. An unexplained dislocation gates to
WATCH with the EV shown — by design. Sub-threshold gaps are VENUE NOISE, not edge:
never describe one as "X points too rich/cheap" — say the market is priced within
normal venue tolerance (a directional view from evidence is "a lean within noise,
not a value call").

For scan questions, CLI-gate every dislocation you headline and fully adjudicate at least
the largest one: ask research "what explains the cheap side?", fold evidence cards back
through `sports_posterior`, and show the final gated verdict. Unexplained dislocations are
candidates, not calls.

### Path-dependent event markets

For tournament winners, group winners, playoff brackets, promotion/relegation,
championship futures, season awards with staged cuts, or any field market where path
matters, do not stop at prediction-market cross-venue spreads or optional book-vs-market
spreads. Build a current-state probability layer; sportsbook odds are optional context,
not a required input.

Default first-pass workflow:

1. Enumerate executable PM/HL boards first, then add bounded sports state/context that
   helps interpret the board: completed results, standings/bracket/cuts if cheap,
   injuries/availability, futures context, and obvious missing path fields.
2. Return the desk-analyst board and value/fade shortlist before any full path model. The
   first pass should include board coverage counts, state classification, and
   `path-model status` such as `not_run_shortlist_first` or `missingPathFields`.
3. Ask `wayfinder-research` only after a first shortlist/model pass unless the user
   explicitly asks for broad qualitative research. It should return a reusable
   `researchInfluencePack`: evidence cards, `researcherOpinion`, `influenceHints`,
   optional `contextPack` / `modelModifiers`, source refs, invalidators, and open
   questions for injuries, lineups, post-line news, rule/resolution mismatch,
   liquidity/depth, lockup/flow, or path/scenario changes. Prose-only research is
   final-synthesis-only and must not be described as consumed by the simulator.
4. For shortlisted candidates, or when the user explicitly asks for full modelling first,
   ask `wayfinder-sports` for a sport-neutral `eventStatePack`: participants,
   ratings/form inputs, completed results, standings/bracket/cuts if known, upcoming path,
   futures_slate fair probabilities if available, and executable PM/HL markets.
   Sportsbook/futures auth failure must not block this pack.
5. Run event_sim validation/smoke before any full simulation: low iterations, short
   timeout, and fail fast on missing group slots, impossible wildcard slots,
   unknown participants, or unsupported target shapes. Generated custom-simulator debugging
   gets one repair max.
6. Run `poetry run python -m wayfinder_paths.quant.event_sim --input <event_pack.json>
   --out .wayfinder_runs/sports` or delegate the pack to `wayfinder-quant`.
7. Price simulated probabilities against executable order-book entries/depth and gate with
   `sports_posterior` when dislocations remain large.

The simulator is one model view, not the answer. Final synthesis must distill multiple
views: executable PM/HL prior, sports/context model or rating model, path simulation, and
qualitative evidence/posterior. Do not present the latest sim as final fair value by
itself. If ratings come from outright winner probabilities or other market-implied prices,
label the sim `diagnostic_only`; if the bracket/path is approximate, label `approx_bracket`
and downgrade buy calls to `WATCH` unless another independent model corroborates them.
Use executable entry/depth for trade math: ask/depth for buys, bid/depth for sells.

Executable sports odds should be shared as TTL'd WorkPacks, not repeatedly re-fetched by
every subagent. The first agent that hydrates PM/HL boards should write a compact sports
`surfacePack` and pass `surfacePackRefs` downstream. Reuse unexpired surface packs for
analysis and final synthesis; refresh only shortlisted markets for exact sizing or
execution. Defaults: PM/HL board `ttlSeconds: 60`, exact quote/depth `ttlSeconds: 30`,
standings/results state `ttlSeconds: 300`. If a worker hits max steps after writing useful
packs, resume from those pack refs; do not improvise a final `BUY` from venue spreads or
qualitative reasoning alone. Missing model/fair-value work should be labeled
`WATCH` / `incomplete_fair_value`.

Run the first-pass board now, even early in an event. Do not defer with "run once the group
stage is 50% complete" or similar. Full path simulation is the second-stage validation
step after the shortlist; if the user explicitly asks for modelling first, skip straight
to the path layer. If the official bracket/path is unavailable for validation, run the
best bounded approximation from known rules and label `pathAssumption: "approximate"`;
if a path cannot be represented at all, surface `missingPathFields` as the blocker.
If a final answer claims fair value without `event_sim` or a custom simulator, label it
`WATCH` / `incomplete_fair_value` and state that simulation is not yet run.

For "scan the field/market" questions, the final answer must be the annotated board before
any single-candidate drilldown. It must include: board coverage counts for each executable
venue, a ranked top-candidate table from the full joined field, each candidate's state
classification, path-model status/artifact (or `missingPathFields`), and order-book depth
only for shortlisted candidates. Never finish with only one or two selected order books.
Under a hard tool budget, use a bounded scan plan and finish anyway: hydrate the main
outright board, search the second executable venue, search group/round boards with enough
candidate coverage to surface multiple event slugs, pull current standings/results using
the canonical sport slug (for World Cup: `worldcup`, not `soccer`) and a generous `limit`
so standings are not silently truncated, then fetch mids for the shortlisted match boards
that surfaced. Prioritize hydrating or directly using group boards for groups with current
results before spending calls on novelty/entertainment match searches. Do not call
`mid_prices` for every encoded outcome in a large field; shortlist top/ambiguous outcomes
first. If group or match boards
surface in search but are not fully hydrated, classify them as `search_surfaced_unhydrated`,
not `not_surfaced`. If group or match boards do not surface, report the search coverage and
move on rather than checkpointing or offering follow-up work.
For broad multi-category scans ("most mispriced across match, group, outright", "scan
the whole field"), cap primary-agent collection at **eight external calls**. Reserve one
call for current state/results with a generous limit and one for match-market mids if match boards surface.
After the eighth call, stop gathering data and write the final answer from the joined
board/state you have. Missing category coverage is a finding (`not_surfaced` /
`search_surfaced_unhydrated` / `missingModelArtifact`), not a reason to continue.
Never output a progress checkpoint for these scans. Avoid progress-only headings such as
`Goal`, `Constraints`, `Progress`, `Done`, `In Progress`, `Blocked`, `Critical Context`,
and `Next Steps` in the answer. A partial annotated board with honest blockers is the
final deliverable.

The generic pack shape is:

```json
{
  "participants": [
    {"id": "stable-id", "name": "Display Name", "rating": 1800,
     "state": "clean_unplayed|live_conditioned|post_result_stale|dead_signal"}
  ],
  "groups": [
    {"id": "Group/Stage", "participants": ["id1", "id2"],
     "qualifiers": [{"rank": 1, "slot": "G1_1"}],
     "matches": [{"a": "id1", "b": "id2", "status": "completed", "score": [1, 0]}]}
  ],
  "wildcards": [{"source_rank": 3, "count": 2, "slot_prefix": "WC"}],
  "bracket": {"matches": [{"id": "m1", "a": {"slot": "G1_1"}, "b": {"participant": "id3"}}],
              "champion_match": "m_final"},
  "target": {"type": "champion|slot|reach_match|match_winner",
             "slots": ["PROMO1", "PROMO2"], "match": "m_final"},
  "markets": [{"participant_id": "id1", "venue": "polymarket|hyperliquid", "bid": 0.01, "ask": 0.011}],
  "modelProvenance": {"ratingSource": "team-strength source", "bracketSource": "official|approximate"}
}
```

Fill only what is known. If the provider/catalog lacks an official path, return
`missingPathFields`; if you approximate from public rules, set `pathAssumption:
"approximate"` and list assumptions. Use `target` to avoid winner-take-all overfitting:
`champion` for outrights, `slot` for promotion/relegation or staged cuts, `reach_match`
for reach-final/reach-playoff markets, and `match_winner` for a specific stage winner. If
`event_sim` cannot represent the sport/event, a quant worker may build a custom simulator,
save artifacts, and document the model and assumptions. Do not create
event-specific prompt rules.

Classify every surfaced candidate:

- `clean_unplayed`: no relevant result has occurred yet.
- `live_conditioned`: current results/standings are folded into the model.
- `post_result_stale`: comparison still uses pre-result odds/model inputs.
- `dead_signal`: path is mathematically gone or economically irrelevant.

Do not call stale or dead signals value.

### LLM forecasting / prediction-market notes

Use LLMs for retrieval, synthesis, evidence cards, `researchInfluencePack` artifacts, and
model selection, not as raw uncalibrated probability oracles. A strong researcher view can
be a visible desk override candidate, but the consuming agent must ledger whether it was
accepted, rejected, or deferred and must show why it changed the final view. Relevant
research patterns:

- Retrieval + aggregation improves LLM forecasting over zero-shot (`Approaching Human-Level Forecasting with Language Models`,
  https://arxiv.org/abs/2402.18563).
- Live prediction-market evaluation needs timestamp-locked order books, news, and
  execution simulation (`PolyBench`, https://arxiv.org/abs/2604.14199).
- Accuracy is not enough; returns depend on execution, downside when wrong, and agreement
  filters (`Beyond Accuracy: Can LLM Forecasters Profit on Prediction Markets?`,
  https://openreview.net/pdf?id=TSA5kRUKZv).
- LLM confidence is often miscalibrated and domain-sensitive; apply posterior gates and
  calibration checks instead of trusting stated confidence (`KalshiBench`,
  https://arxiv.org/abs/2512.16030).

The pipelines are multi-sport (NBA/NHL/MLB/World Cup verified live). They are context and
model helpers, not gates before PM/HL pricing. MLB notes: props
include one-sided "milestone" quotes (single odds, no under side) — the pipeline skips
these with a visible count; do NOT model them by hand (a single quote cannot be
de-vigged). MLB pitcher props project off outs recorded, batter props off plate
appearances. Soccer notes: moneylines are three-way (1X2 — home/draw/away de-vigged
together; never two-way over home/away); futures quotes carry the whole field's vig
(de-vig across the entire field, never read one quote as a probability); a brand-new
tournament has no completed-game form — game_slate flags `no_form_model` and shows
odds-only views.

One command: fetches props + complete paginated game logs + team pace/defense + injuries,
models with proper distributions and optional book-context probabilities, and prints an
`ACTIONABLE` / `WATCH` (flagged) / `EXCLUDED` (no joinable data) table; writes
`prop_slate_<game>.csv/.json` artifacts. Output fields per pick: `model_p`, `book_p`,
`book_edge`, `book_ev`, `kelly`, `proj`, `n`, `flags`. `book_*` numbers are vs optional
non-executable sportsbook context — informational; the executable stage is
`sports_props.market_edge(pick.model_p, polymarket_price)` against a matching Polymarket market.

For custom analysis the pipeline doesn't cover (matchup deep-dives, soccer xG, cross-game
studies), write a bounded script. Fetch through `SPORTS_CLIENT` (same backend gateway as the MCP
tools: key-safe, allowlisted, cached — never raw provider URLs), shape with pandas, model with
the quant modules:

```python
import asyncio, pandas as pd
from wayfinder_paths.core.clients.SportsClient import SPORTS_CLIENT
from wayfinder_paths.quant import sports_props as sp          # projections, EV, market_edge
# from wayfinder_paths.quant import polymarket_edge           # prediction-market math

async def main():
    logs = await SPORTS_CLIENT.provider_call(
        endpoint_id="data.player_stats.list", sport="nba",
        query={"player_ids": [161, 1057262518], "seasons": [2025], "per_page": 100})
    df = pd.DataFrame(logs["data"])
    # rolling form, hit rates vs a line, joins to team pace/def_rating, score_prop EV tables...
    df.to_csv(".wayfinder_runs/sports/analysis.csv", index=False)  # artifact -> dataFiles

asyncio.run(main())
```

`SPORTS_CLIENT` methods: `snapshot(action=..., sport=..., ...)`,
`provider_call(endpoint_id=..., sport=..., path_params=..., query=..., body=..., run_id=...)`,
`provider_catalog()`, `backtest_state(action=..., run_id=...)` — all async. Conventions: bulk
arrays over per-player loops; bounded lookbacks (a season, not all history); big tables go to
`.wayfinder_runs/sports/` artifacts (return the paths), summaries go in the response.

Local sports scripts are optional accelerators, not blockers. If a script or direct
`SPORTS_CLIENT` call fails with auth/config errors (`401`, `403`, missing key/config,
provider entitlement), do not retry or defer. Mark the model artifact as
`script_auth_unavailable`, use the already gathered MCP snapshots/PM/HL boards, and produce
a final answer with lower confidence plus explicit `missingPathFields` /
`missingModelArtifact`. For outrights/path markets, still return an `eventStatePack` from
known participants/current state/executable PM/HL markets even when `futures_slate` or
sportsbook futures fail auth. Never turn a script-auth failure into a checkpoint or
follow-up.

Hard rules for prop scoring scripts (learned from real runs):
- Use `sports_props` for the math — `devig_two_way` (never compare against raw vigged implied
  probabilities), `score_prop`/`project_stat`/`prob_over` (proper distributions + shrinkage),
  `prop_value`/`market_edge`. Don't reimplement a simpler model inline.
- `per_page=100` does not hold a slate's season of logs — chunk `player_ids` or follow
  `meta.next_cursor` until every player has rows.
- A player with zero joined logs is a broken join/pagination, not a 0.0 average — exclude or
  refetch, and sanity-check stars' averages before ranking.
- Run with `poetry run python` (plain `python3` lacks pandas and project deps).

## Lab (backtesting) quick sheet — nba/nfl/nhl/mlb only

- Factors: `lab.factors.list` — integer `factor_id`, `slug` (`pp_*` = player-prop factors),
  typed `configurable_params`.
- Create: `lab.models.create` body `{name, sport, bet_type: moneyline|spread|over_under,
  mode: simple|weighted, factors: [{factor_id, parameters, weight}]}` — the key is
  **`parameters`** (not `params`); weighted weights **sum to exactly 100**; prop models need
  `model_type: "player_prop"` + `prop_type` + `pp_*` factors (game models reject `pp_*`).
- Update replaces (PUT semantics — send the full body). Backtest: `lab.performance.run`
  (async job) → `lab.performance.get` (win_rate, roi as a fraction, results_by_confidence).
- Predictions: `lab.predictions.generate` (model id in path) → list via
  `query={"model_id": ...}` (top-level route). Active jobs for a model: `lab.jobs.active`
  (`path_params.id` = model id). Models/jobs are **scoped to your workspace** — foreign ids 404.
- Jobs are async (`pending → running → completed/failed`): kick off, capture
  `run_id`/`job_id`, monitor via `wayfinder_sports_backtest_state` — never tight-loop.

## Eval Variant: Generic WorkPack Protocol

Use generic WorkPack names across sports, prediction markets, swaps, DeFi,
assets, and perps: `surfacePack`, `contextPack`, `featurePack`,
`analysisPack`, `decisionPack`, and `validationReport`.

Legacy sports fields map as follows: `eventStatePack` maps to sports
context/feature packs for path markets, `dataFiles` maps to `artifactRefs`,
and `contextForNextAgent` remains routing metadata. Persist packs for audit,
reuse, repair, and cross-agent handoff, but never treat persisted prices,
order books, quotes, balances, or executable depth as live truth. Rehydrate
PM/HL executable surfaces before `recommend_buy`, `place_order`, or execute.

Unsupported markets/fights should return a clean missing/unsupported pack
status. Do not invent odds, fight stats, market availability, or a
recommendation.
