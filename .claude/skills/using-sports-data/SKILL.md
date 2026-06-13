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
| `data.shots.list` | soccer shot maps with **xG** | `query.game_id` |
| `data.match_events.list` | soccer goals/cards/subs | `query.game_id` |
| `data.momentum.list`, `data.pregame_forms.list` | soccer momentum / recent form | `query.game_id` |
| `data.rosters.list` | rosters; NFL depth charts | soccer: `query`; NFL: `path_params.team_id` |
| `data.results.list` | F1 session results / PGA tournament results / MMA fight results | `query`: season/event filters |
| `data.qualifying.list`, `data.pit_stops.list`, `data.laps.list` | F1 detail (plan-gated upstream) | `query` session/event ids |
| `data.venues.list` | circuits / stadiums / courses | — |
| `data.round_stats.list` | PGA strokes-gained round stats | `query` tournament/player |
| `data.splits.list`, `data.plate_appearances.list` | MLB splits / plate appearances | `query` player/season |
| `data.pitcher_pitch_stats.list`, `data.hitter_pitch_stats.list` | MLB pitch-type breakdowns | `query` player/season |
| `data.conferences.list`, `data.bracket.list` | college conferences / March Madness bracket | `query.season` |
| `data.player_contracts.list`, `data.team_contracts.list` | NBA salaries/payroll (plan-gated upstream) | `query` |
| `data.shot_locations.list` | WNBA shooting zones | `query` player/season |
| `data.odds.list` | game odds (spread/moneyline/total) | `query`: `game_id` OR `date` (NBA accepts arrays) |
| `data.player_props.list` | player prop lines + over/under odds | `query.game_id` (required) |
| `data.futures.list` | outright/futures odds | `query.season` |

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

Sportsbook odds/props/futures are **context, never the executable price** — Wayfinder executes
sports bets only on Polymarket. Compute edges with
`wayfinder_paths.quant.sports_props.market_edge(model_p, polymarket_price)` against
`polymarket_read` order-book prices.

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
- **Game-scoped resources** need an event id: `query={"game_id": ...}` (or `path_params.id`).
- **Caching**: non-live data (stats/averages/rosters) is cached server-side for hours — repeats
  are cheap; odds/props/futures stay near-live (~15s). Still batch.
- **Pagination**: `per_page` (max 100) + cursor in `meta.next_cursor` where present.

## Scripted analysis (inside `core_run_script`)

**For betting analysis, run the canned pipelines for DATA + MARKET MATH (complete
fetches, de-vig, consensus, dislocation gating — correctness you must not hand-roll;
never pull odds from the web). MODELING is the agent's judgment: `game_slate` separates
an INFORMATION section (facts) from a labeled REFERENCE MODEL (one opinion — adjust or
replace it; `--data-only` for facts alone), and your own view is expressed as evidence
cards gated through `sports_posterior` over the executable prior:**

```
# player props -> ACTIONABLE/WATCH/EXCLUDED EV table
poetry run python -m wayfinder_paths.quant.prop_slate \
  --sport nba --game-id <GAME_ID> --season <SEASON> --out .wayfinder_runs/sports

# game markets (moneyline/total/spread) -> model vs consensus de-vigged books
# (+ the provider's polymarket vendor line as the quasi-executable reference)
poetry run python -m wayfinder_paths.quant.game_slate \
  --sport nhl --game-id <GAME_ID> --season <SEASON> --date <GAME_DATE> --out .wayfinder_runs/sports
```

```
# futures fields (tournament winner / group winner / reach-final) -> de-vigged fair_p per candidate
poetry run python -m wayfinder_paths.quant.futures_slate \
  --sport worldcup --market-type outright --out .wayfinder_runs/sports

# book-fair vs Polymarket disagree? -> dislocation check + gated posterior ledger
poetry run python -m wayfinder_paths.quant.sports_posterior \
  --market <PM_PRICE> --book <FAIR_P> --vendors <N> --overround <O> \
  [--card "davies_out:against:medium:news"]
```

**Executable board rule:** Polymarket lists a per-game EVENT (slug
`{league}-{away}-{home}-{YYYY-MM-DD}`, e.g. `mlb-lad-cws-2026-06-12`) carrying a whole
board — alternate spreads/totals, first-half/F5 lines, game props. Hydrate it
(`polymarket_read get_event`) and enumerate its markets as the executable candidate
set; `game_slate` emits `alt_lines` (model probabilities for the alt ladder) to price
them. "No provider props" never means "nothing executable."

**Dislocation rule:** when a slate's de-vigged book number and the Polymarket price for
the same outcome disagree enough that `sports_posterior.dislocation` flags it, never
recommend the cheap side on trust — the prior is the EXECUTABLE price, the book number
enters as one capped evidence card, and the gap must be adjudicated (research: "what
explains the cheap side?" — post-line news, resolution-rules mismatch, lockup/flow,
de-vig method risk) before any recommendation. An unexplained dislocation gates to
WATCH with the EV shown — by design. Sub-threshold gaps are VENUE NOISE, not edge:
never describe one as "X points too rich/cheap" — say the market is priced within
normal venue tolerance (a directional view from evidence is "a lean within noise,
not a value call").

The pipelines are multi-sport (NBA/NHL/MLB/World Cup verified live). MLB notes: props
include one-sided "milestone" quotes (single odds, no under side) — the pipeline skips
these with a visible count; do NOT model them by hand (a single quote cannot be
de-vigged). MLB pitcher props project off outs recorded, batter props off plate
appearances. Soccer notes: moneylines are three-way (1X2 — home/draw/away de-vigged
together; never two-way over home/away); futures quotes carry the whole field's vig
(de-vig across the entire field, never read one quote as a probability); a brand-new
tournament has no completed-game form — game_slate flags `no_form_model` and shows
odds-only views.

One command: fetches props + complete paginated game logs + team pace/defense + injuries,
models with proper distributions and de-vigged book probabilities, and prints an
`ACTIONABLE` / `WATCH` (flagged) / `EXCLUDED` (no joinable data) table; writes
`prop_slate_<game>.csv/.json` artifacts. Output fields per pick: `model_p`, `book_p`,
`book_edge`, `book_ev`, `kelly`, `proj`, `n`, `flags`. `book_*` numbers are vs the de-vigged
SPORTSBOOK odds — informational; the executable stage is
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
