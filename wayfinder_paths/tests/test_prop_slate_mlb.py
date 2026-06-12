"""MLB generalization of the prop-slate pipeline: nested over/under + milestone markets,
plate-appearance / pitching-out exposure families, derived singles, game_id ordering."""

import pytest

from wayfinder_paths.quant import prop_slate as ps
from wayfinder_paths.quant import sports_props as sp


def _gw(rows, next_cursor=None):
    return {"status": 200, "data": {"data": rows, "meta": {"next_cursor": next_cursor}}}


class StubClient:
    def __init__(self, queues):
        self.queues = {k: list(v) for k, v in queues.items()}
        self.calls = []

    async def provider_call(self, **kwargs):
        self.calls.append(kwargs)
        queue = self.queues.get(kwargs["endpoint_id"], [])
        item = queue.pop(0) if queue else _gw([])
        if isinstance(item, Exception):
            raise item
        return item


def _bat_log(pid, gid, *, name="Shohei Ohtani", team="Los Angeles Dodgers", **stats):
    base = {
        "player": {"id": pid, "first_name": name.split()[0], "last_name": name.split()[1]},
        "game_id": gid,
        "team_name": team,
        "plate_appearances": 4,
        "at_bats": 4,
        "hits": 1,
        "doubles": 0,
        "triples": 0,
        "hr": 0,
        "rbi": 1,
        "runs": 0,
        "bb": 0,
        "stolen_bases": 0,
        "total_bases": 1,
        "pitching_outs": 0,
        "p_k": 0,
        "er": 0,
        "p_hits": 0,
    }
    base.update(stats)
    return base


def _ou(pid, prop_type, line, over, under, vendor="fanduel"):
    return {
        "player_id": pid,
        "prop_type": prop_type,
        "line_value": str(line),
        "vendor": vendor,
        "market": {"type": "over_under", "over_odds": over, "under_odds": under},
    }


def _milestone(pid, prop_type, line, odds, vendor="fanduel"):
    return {
        "player_id": pid,
        "prop_type": prop_type,
        "line_value": str(line),
        "vendor": vendor,
        "market": {"type": "milestone", "odds": odds},
    }


# ── sports_props: MLB mappings + exposure ────────────────────────────────────


def test_mlb_prop_stats_mapped_and_poisson():
    assert sp.stat_keys("hits") == ("hits",)
    assert sp.stat_keys("hits_runs_rbis") == ("hits", "runs", "rbi")
    assert sp.stat_keys("pitcher_strikeouts") == ("p_k",)
    assert sp.pick_distribution("hits") == "poisson"
    assert sp.pick_distribution("pitcher_outs") == "normal"  # mean ~15-18


def test_project_stat_uses_exposure_key_and_splits_two_way():
    # Two-way player: 6 batting games (1 hit each, 4 PA) + 2 pitching games (6 K, 18 outs;
    # 0 PA so they must NOT count as batting appearances).
    logs = [
        *[
            {"plate_appearances": 4, "hits": 1, "pitching_outs": 0, "p_k": 0}
            for _ in range(6)
        ],
        *[
            {"plate_appearances": 0, "hits": 0, "pitching_outs": 18, "p_k": 6}
            for _ in range(2)
        ],
    ]
    bat = sp.project_stat(logs, None, ("hits",), exposure_key="plate_appearances")
    assert bat.n == 6  # pitching-only games excluded from the batting sample
    assert bat.mean == pytest.approx(1.0, abs=0.05)
    arm = sp.project_stat(logs, None, ("p_k",), exposure_key="pitching_outs")
    assert arm.n == 2
    assert arm.mean == pytest.approx(6.0, abs=0.5)


def test_exposure_key_for_routing():
    assert ps.exposure_key_for("mlb", "hits") == "plate_appearances"
    assert ps.exposure_key_for("mlb", "pitcher_strikeouts") == "pitching_outs"
    assert ps.exposure_key_for("nba", "points") == "min"


# ── pipeline fetch + score on stub MLB data ──────────────────────────────────


def _mlb_stub(prop_rows, logs):
    return StubClient(
        {
            "data.event.get": [
                {
                    "status": 200,
                    "data": {
                        "data": {
                            "id": 5058809,
                            "home_team": {
                                "id": 6,
                                "abbreviation": "CHW",
                                "display_name": "Chicago White Sox",
                            },
                            "away_team": {
                                "id": 14,
                                "abbreviation": "LAD",
                                "display_name": "Los Angeles Dodgers",
                            },
                        }
                    },
                }
            ],
            "data.player_props.list": [_gw(prop_rows)],
            "data.player_stats.list": [_gw(logs)],
            "data.team_season_averages.list": [RuntimeError("no advanced surface")],
            "data.injuries.list": [_gw([])],
        }
    )


@pytest.mark.asyncio
async def test_mlb_slate_scores_nested_markets_and_skips_milestones():
    props = [
        _ou(208, "hits", 1.5, 130, -160),
        _ou(208, "total_bases", 1.5, -110, -110),
        _milestone(208, "home_runs", 0.5, 320),  # one-sided: must be skipped, visibly
        _milestone(208, "stolen_bases", 0.5, 400),
    ]
    # 12 games, most recent (higher game_id) hot: 2 hits; older: 1 hit.
    logs = [
        _bat_log(208, 5040000 + i, hits=2 if i >= 9 else 1, total_bases=2 if i >= 9 else 1)
        for i in range(12)
    ]
    slate = await ps.fetch_prop_slate("mlb", 5058809, 2026, client=_mlb_stub(props, logs), pace_s=0)

    assert slate.skipped_one_sided == 2
    assert {p["prop_type"] for p in slate.props} == {"hits", "total_bases"}
    # baseline is per exposure family with batting games only
    assert slate.season_baseline[208]["plate_appearances"] == pytest.approx(4.0)
    assert "pitching_outs" not in slate.season_baseline[208]
    # logs sorted most-recent-first by game_id (MLB rows carry no game date)
    assert slate.logs_by_player[208][0]["game_id"] == 5040011
    # singles derived onto each row
    assert slate.logs_by_player[208][0]["singles"] == pytest.approx(2.0)
    # team label resolved from team_name -> event abbreviation
    assert slate.player_team[208] == "LAD"

    result = ps.score_prop_slate(slate)
    picks = {p.prop_type: p for p in result.actionable + result.watch}
    assert "hits" in picks and "total_bases" in picks
    assert picks["hits"].team == "LAD"
    assert result.skipped_one_sided == 2
    assert ps.render_slate(result)  # renders with the SKIPPED line
    assert "one-sided" in ps.render_slate(result)
    # neutral factors when no advanced team surface exists
    assert result.pace_factor == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_mlb_pitcher_prop_projects_off_outs():
    props = [_ou(6032, "pitcher_strikeouts", 4.5, 112, -138)]
    # Starter: 10 appearances, 18 outs + 6 K each; sandwiched between 20 batting-only
    # games where he didn't pitch (must not drag the K rate to zero).
    logs = []
    gid = 5040000
    for i in range(30):
        gid += 1
        if i % 3 == 0:
            logs.append(
                _bat_log(6032, gid, name="Garrett Crochet", team="Chicago White Sox",
                         plate_appearances=0, hits=0, pitching_outs=18, p_k=6)
            )
        else:
            logs.append(
                _bat_log(6032, gid, name="Garrett Crochet", team="Chicago White Sox",
                         plate_appearances=2, hits=0)
            )
    slate = await ps.fetch_prop_slate("mlb", 5058809, 2026, client=_mlb_stub(props, logs), pace_s=0)
    result = ps.score_prop_slate(slate)
    pick = (result.actionable + result.watch)[0]
    assert pick.prop_type == "pitcher_strikeouts"
    assert pick.n_games == 10  # only games he actually pitched
    assert pick.proj_mean == pytest.approx(6.0, abs=0.5)
    assert pick.side == "OVER"  # projecting 6 K vs a 4.5 line
    assert pick.team == "CHW"
