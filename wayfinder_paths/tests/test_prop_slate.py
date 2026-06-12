import pytest

from wayfinder_paths.quant import prop_slate as ps
from wayfinder_paths.quant import sports_props as sp


def _gw(rows, next_cursor=None):
    """Gateway-shaped payload: {data: {data: [...], meta: {...}}}."""
    return {"status": 200, "data": {"data": rows, "meta": {"next_cursor": next_cursor}}}


class StubClient:
    """Queues responses per endpoint_id; records calls."""

    def __init__(self, queues):
        self.queues = {k: list(v) for k, v in queues.items()}
        self.calls = []

    async def provider_call(self, **kwargs):
        self.calls.append(kwargs)
        queue = self.queues.get(kwargs["endpoint_id"], [])
        return queue.pop(0) if queue else _gw([])


def _prop_row(pid, prop_type, line, vendor="bookA", over=-110, under=-110):
    return {
        "player_id": pid,
        "prop_type": prop_type,
        "line_value": str(line),
        "vendor": vendor,
        "market": {"type": "over_under", "over_odds": over, "under_odds": under},
    }


def _log(pid, name, team_id, date, pts, mins="32:00"):
    return {
        "player": {"id": pid, "first_name": name, "last_name": name},
        "team": {"id": team_id},
        "game": {"date": date},
        "min": mins,
        "pts": pts,
        "reb": 4,
        "ast": 3,
        "stl": 1,
        "blk": 0,
        "fg3m": 2,
        "turnover": 2,
    }


def test_select_vendor_picks_max_coverage():
    rows = [
        _prop_row(1, "points", 20.5, vendor="bookA"),
        _prop_row(1, "rebounds", 5.5, vendor="bookA"),
        _prop_row(1, "points", 20.5, vendor="bookB"),
    ]
    assert ps.select_vendor(rows) == "bookA"


@pytest.mark.asyncio
async def test_fetch_follows_cursor_and_excludes_missing_players():
    event = {
        "status": 200,
        "data": {"data": {"home_team": {"id": 10}, "visitor_team": {"id": 20}}},
    }
    props = _gw([_prop_row(1, "points", 20.5), _prop_row(2, "points", 10.5)])
    # player 1's logs split across two cursor pages; player 2 returns nothing (twice)
    page1 = _gw([_log(1, "Fox", 10, "2026-01-02", 25)], next_cursor=999)
    page2 = _gw([_log(1, "Fox", 10, "2026-01-01", 21)])
    teams = _gw(
        [
            {
                "team": {"id": 10, "abbreviation": "SAS"},
                "stats": {"pace": 102, "def_rating": 110},
            },
            {
                "team": {"id": 20, "abbreviation": "NYK"},
                "stats": {"pace": 98, "def_rating": 106},
            },
        ]
    )
    client = StubClient(
        {
            "data.event.get": [event],
            "data.player_props.list": [props],
            "data.player_stats.list": [page1, page2, _gw([]), _gw([])],
            "data.team_season_averages.list": [teams],
            "data.injuries.list": [_gw([])],
        }
    )
    slate = await ps.fetch_prop_slate("nba", 99, 2025, client=client, pace_s=0)

    # cursor was followed: second stats call carried it
    stats_calls = [
        c for c in client.calls if c["endpoint_id"] == "data.player_stats.list"
    ]
    assert stats_calls[1]["query"].get("cursor") == 999
    # player 1 has both pages, most-recent-first
    assert [lg["pts"] for lg in slate.logs_by_player[1]] == [25, 21]
    # player 2: refetched individually, still empty -> excluded with reason (never 0.0-scored)
    assert {"player_id": 2, "reason": "no_game_logs"} in slate.excluded
    # season baseline derived from ALL logs (mean of 25 and 21)
    assert slate.season_baseline[1]["pts"] == pytest.approx(23.0)
    assert slate.opponent_of[10] == 20 and slate.opponent_of[20] == 10


def _slate_for_scoring(n_games, pts=30.0, line=20.5):
    logs = [_log(1, "Hot", 10, f"2026-01-{i + 1:02d}", pts) for i in range(n_games)]
    return ps.SlateData(
        sport="nba",
        game_id=99,
        season=2025,
        vendor="bookA",
        props=[
            {
                "player_id": 1,
                "prop_type": "points",
                "line": line,
                "over_odds": -110,
                "under_odds": -110,
            }
        ],
        logs_by_player={1: logs},
        season_baseline={1: {"pts": pts, "min": 32.0}},
        player_names={1: "H. Hot"},
        player_team={1: 10},
        opponent_of={10: 20, 20: 10},
        team_stats={
            10: {"pace": 100, "def_rating": 113, "abbreviation": "AAA"},
            20: {"pace": 100, "def_rating": 113, "abbreviation": "BBB"},
        },
        league_pace=100.0,
        league_def_rating=113.0,
        injured=set(),
        excluded=[{"player_id": 2, "reason": "no_game_logs"}],
    )


def test_scoring_devigs_and_partitions():
    # 12 clean games -> no low_sample; big projection vs line -> suspect_edge -> watch bucket
    result = ps.score_prop_slate(_slate_for_scoring(12))
    assert not result.actionable
    assert len(result.watch) == 1
    pick = result.watch[0]
    assert "suspect_edge" in pick.flags
    # de-vig applied: book_p for -110/-110 is exactly 0.5 (raw implied would be 0.524)
    assert pick.book_p == pytest.approx(0.5)
    assert pick.model_p > 0.5 and pick.side == "OVER"
    # excluded players surface in the result untouched
    assert result.excluded[0]["reason"] == "no_game_logs"


def test_low_sample_goes_to_watch_and_sane_edge_actionable():
    # 5 games -> low_sample flag -> watch
    low = ps.score_prop_slate(_slate_for_scoring(5))
    assert low.watch and "low_sample(5)" in low.watch[0].flags
    # 12 games with a line near the projection -> sane edge, clean -> actionable
    sane = ps.score_prop_slate(_slate_for_scoring(12, pts=21.0, line=20.5))
    assert sane.actionable and not sane.actionable[0].flags
    assert abs(sane.actionable[0].book_edge) <= ps.SUSPECT_EDGE


def test_render_includes_two_stage_note_and_exclusions():
    result = ps.score_prop_slate(_slate_for_scoring(12))
    text = ps.render_slate(result)
    assert "Polymarket" in text and "market_edge" in text  # two-stage note
    assert "EXCLUDED" in text and "no_game_logs" in text
    rows = ps.slate_rows(result)
    assert rows and rows[0]["bucket"] in ("actionable", "watch")
    assert "model_p" in rows[0]  # the executable-stage input is always exported


def test_book_p_matches_devig_helper():
    over, under = -129, -106
    expected_over_p, _ = sp.devig_two_way(over, under)
    slate = _slate_for_scoring(12, pts=21.0, line=20.5)
    slate.props[0]["over_odds"], slate.props[0]["under_odds"] = over, under
    result = ps.score_prop_slate(slate)
    pick = (result.actionable + result.watch)[0]
    book_p_over = pick.book_p if pick.side == "OVER" else 1 - pick.book_p
    assert book_p_over == pytest.approx(expected_over_p, abs=1e-4)
