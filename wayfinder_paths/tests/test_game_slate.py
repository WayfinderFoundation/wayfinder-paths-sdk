import pytest

from wayfinder_paths.quant import game_slate as gs


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


def _nhl_event(gid, home_id, away_id, hs, as_, date, state="OFF"):
    return {
        "id": gid,
        "game_date": date,
        "game_state": state,
        "home_team": {
            "id": home_id,
            "full_name": f"H{home_id}",
            "tricode": f"H{home_id}",
        },
        "away_team": {
            "id": away_id,
            "full_name": f"A{away_id}",
            "tricode": f"A{away_id}",
        },
        "home_score": hs,
        "away_score": as_,
    }


def _odds_row(
    vendor,
    ml_h,
    ml_a,
    total="5.5",
    over=-110,
    under=-110,
    sh_val="1.5",
    sh=-250,
    sa=200,
):
    return {
        "vendor": vendor,
        "moneyline_home_odds": ml_h,
        "moneyline_away_odds": ml_a,
        "total_value": total,
        "total_over_odds": over,
        "total_under_odds": under,
        "spread_home_value": sh_val,
        "spread_home_odds": sh,
        "spread_away_value": "-1.5",
        "spread_away_odds": sa,
    }


# ── models ───────────────────────────────────────────────────────────────────


def test_poisson_game_probs_coherent():
    p = gs.poisson_game_probs(3.4, 2.6, total_line=5.5, spread_line=1.5)
    assert p["home_ml"] + p["away_ml"] == pytest.approx(1.0, abs=1e-6)
    assert p["home_ml"] > 0.5  # stronger attack at home
    assert 0 < p["over"] < 1
    assert p["home_spread"] > p["home_ml"]  # +1.5 covers more outcomes than winning


def test_poisson_total_monotonic_in_rates():
    low = gs.poisson_game_probs(2.5, 2.2, total_line=5.5, spread_line=None)["over"]
    high = gs.poisson_game_probs(3.6, 3.2, total_line=5.5, spread_line=None)["over"]
    assert high > low


def test_normal_game_probs_coherent():
    p = gs.normal_game_probs(
        112, 108, total_line=219.5, spread_line=4.5, margin_sigma=12.5, total_sigma=19
    )
    assert p["home_ml"] > 0.5
    assert p["home_ml"] + p["away_ml"] == pytest.approx(1.0)
    assert 0.4 < p["over"] < 0.7


# ── odds parsing ─────────────────────────────────────────────────────────────


def test_parse_game_odds_consensus_and_polymarket_vendor():
    rows = [
        _odds_row("fanduel", -110, -110),
        _odds_row("draftkings", -115, -105),
        _odds_row("polymarket", 111, -111),
    ]
    markets = gs.parse_game_odds(rows)
    ml = markets["moneyline"]
    assert ml["n_vendors"] == 3
    assert 0.45 < ml["home_p"] < 0.55  # consensus near coin-flip, de-vigged
    assert markets["total"]["line"] == 5.5
    assert markets["spread"]["home_line"] == 1.5
    pm = markets["polymarket_vendor"]
    assert pm["home_ml_p"] < 0.5  # +111 home underdog at the polymarket vendor


def test_event_shape_normalization():
    nhl = _nhl_event(1, 10, 20, 4, 2, "2026-06-01")
    nba = {
        "id": 2,
        "date": "2026-06-01",
        "status": "Final",
        "home_team": {"id": 5, "abbreviation": "AAA"},
        "visitor_team": {"id": 6, "abbreviation": "BBB"},
        "home_team_score": 100,
        "visitor_team_score": 98,
    }
    assert gs.event_completed(nhl) and gs.event_completed(nba)
    assert gs.event_scores(nba) == (100, 98)
    assert gs.event_teams(nba)[1]["id"] == 6
    future = _nhl_event(3, 10, 20, 0, 0, "2026-06-14", state="FUT")
    assert not gs.event_completed(future)


# ── fetch + score (stub) ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_falls_back_to_date_lookup_and_flags():
    target = _nhl_event(99, 10, 20, 0, 0, "2026-06-14", state="FUT")
    home_games = _gw(
        [_nhl_event(i, 10, 30 + i, 4, 2, f"2026-05-{i + 1:02d}") for i in range(10)]
    )
    away_games = _gw(
        [
            _nhl_event(50 + i, 40 + i, 20, 2, 3, f"2026-05-{i + 1:02d}")
            for i in range(10)
        ]
    )
    client = StubClient(
        {
            "data.event.get": [RuntimeError("Route not found")],
            "data.events.list": [_gw([target]), home_games, away_games],
            "data.odds.list": [_gw([_odds_row("fanduel", -110, -110)])],
        }
    )
    slate = await gs.fetch_game_slate(
        "nhl", 99, 2025, date="2026-06-14", client=client, pace_s=0
    )
    assert slate.home["id"] == 10 and slate.away["id"] == 20
    assert slate.home_form["n"] == 10 and slate.home_form["for"] == pytest.approx(4.0)
    assert slate.away_form["against"] == pytest.approx(2.0)  # away team conceded 2/g
    assert "no_provider_odds" not in slate.flags

    result = gs.score_game_slate(slate)
    markets = {v.market: v for v in result.views}
    assert (
        result.lam_home > result.lam_away
    )  # home scores 4/g + away concedes 2... > away
    assert markets["moneyline_home"].model_p > 0.5
    assert markets["moneyline_home"].book_p == pytest.approx(0.5, abs=0.01)
    assert markets["over"].line == 5.5
    # 10 games < MIN_GAMES=8? 10 >= 8 -> no low_sample
    assert not any("low_sample" in f for f in markets["moneyline_home"].flags)


@pytest.mark.asyncio
async def test_missing_odds_yields_model_only_with_flag():
    target = _nhl_event(99, 10, 20, 0, 0, "2026-06-14", state="FUT")
    games = _gw([_nhl_event(i, 10, 30, 3, 3, f"2026-05-{i + 1:02d}") for i in range(5)])
    client = StubClient(
        {
            "data.event.get": [_gw([])],  # empty -> triggers date fallback
            "data.events.list": [_gw([target]), games, _gw([])],
            "data.odds.list": [RuntimeError("boom")],
        }
    )
    slate = await gs.fetch_game_slate(
        "nhl", 99, 2025, date="2026-06-14", client=client, pace_s=0
    )
    assert "no_provider_odds" in slate.flags
    assert any("low_sample" in f for f in slate.flags)  # 5 and 0 games
    result = gs.score_game_slate(slate)
    ml = next(v for v in result.views if v.market == "moneyline_home")
    assert ml.book_p is None and ml.book_edge is None  # model-only, never fabricated
    assert gs.render_game(result)  # renders without odds


def test_render_includes_two_stage_note():
    slate = gs.GameSlate(
        sport="nhl",
        game_id=1,
        season=2025,
        home={"id": 1, "tricode": "VGK"},
        away={"id": 2, "tricode": "CAR"},
        home_form={"for": 3.0, "against": 3.0, "n": 20},
        away_form={"for": 3.5, "against": 2.5, "n": 20},
        markets=gs.parse_game_odds([_odds_row("polymarket", 111, -111)]),
    )
    text = gs.render_game(gs.score_game_slate(slate))
    assert "market_edge" in text and "Polymarket" in text
    assert "polymarket vendor line" in text
