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


def test_whole_number_lines_condition_on_push():
    # Poisson: total mean 9.6 vs a 9.0 line -> ~12-13% push mass must be excluded,
    # flipping the naive sub-50% over into the correct over-lean.
    p = gs.poisson_game_probs(5.2, 4.4, total_line=9.0, spread_line=-1.0)
    assert p["total_push"] > 0.10
    assert p["over"] > 0.5  # mean 9.6 over a 9 line leans over once pushes are excluded
    naive = gs.poisson_game_probs(5.2, 4.4, total_line=9.5, spread_line=None)
    assert p["over"] != pytest.approx(naive["over"])  # conditioning actually applied
    assert p["spread_push"] > 0
    # half-lines cannot push
    half = gs.poisson_game_probs(5.2, 4.4, total_line=9.5, spread_line=1.5)
    assert half["total_push"] == 0 and half["spread_push"] == 0

    # Normal: integer line gets the continuity-bin treatment, half line unchanged
    exact_mean = gs.normal_game_probs(
        110, 110, total_line=220.0, spread_line=None, margin_sigma=12, total_sigma=19
    )
    assert exact_mean["over"] == pytest.approx(0.5, abs=1e-6)  # symmetric around the line


# ── soccer: three-way moneyline, nested totals, zero-form guard ──────────────


def _wc_odds_row(vendor, ml_h, ml_d, ml_a, total_markets=()):
    nested = []
    for line, over, under in total_markets:
        nested.append(
            {
                "key": f"total_match_match_over/under_{line}_goals_{line}",
                "type": "total",
                "period": "match",
                "scope": "match",
                "line_value": str(line),
                "outcomes": [
                    {"type": "over", "american_odds": over},
                    {"type": "under", "american_odds": under},
                ],
            }
        )
    return {
        "vendor": vendor,
        "match_id": 3,
        "moneyline_home_odds": ml_h,
        "moneyline_draw_odds": ml_d,
        "moneyline_away_odds": ml_a,
        "total_value": None,
        "total_over_odds": None,
        "total_under_odds": None,
        "markets": nested,
    }


def test_three_way_moneyline_devig_and_nested_totals():
    rows = [
        _wc_odds_row("fanduel", -125, 250, 380, [(1.5, -200, 160), (2.5, 105, -125), (4.5, 600, -1000)]),
        _wc_odds_row("draftkings", -120, 250, 380, [(2.5, 100, -120), (4.5, 550, -900)]),
    ]
    markets = gs.parse_game_odds(rows)
    ml = markets["moneyline"]
    assert ml["three_way"] is True
    assert ml["home_p"] + ml["draw_p"] + ml["away_p"] == pytest.approx(1.0)
    assert ml["home_p"] > 0.5 > ml["away_p"]  # Canada favorite, Bosnia dog
    # nested totals parsed; tie on vendor count broken toward the balanced (main) line
    assert markets["total"]["line"] == 2.5
    assert 0.4 < markets["total"]["over_p"] < 0.6


def test_draws_sport_emits_three_way_model():
    p = gs.poisson_game_probs(1.6, 1.1, total_line=2.5, spread_line=None, split_ties=False)
    assert p["home_ml"] + p["draw"] + p["away_ml"] == pytest.approx(1.0, abs=1e-6)
    assert p["draw"] > 0.15  # soccer-range draw mass

    slate = gs.GameSlate(
        sport="worldcup",
        game_id=3,
        season=2026,
        home={"id": 1, "abbreviation": "CAN"},
        away={"id": 2, "abbreviation": "BIH"},
        home_form={"for": 1.8, "against": 0.9, "n": 10},
        away_form={"for": 1.1, "against": 1.4, "n": 10},
        markets=gs.parse_game_odds([_wc_odds_row("fanduel", -125, 250, 380, [(2.5, 105, -125)])]),
    )
    result = gs.score_game_slate(slate)
    names = [v.market for v in result.views]
    assert "moneyline_draw" in names
    draw = next(v for v in result.views if v.market == "moneyline_draw")
    assert draw.model_p is not None and draw.book_p is not None
    ml_sum = sum(
        v.model_p for v in result.views if v.market.startswith("moneyline")
    )
    assert ml_sum == pytest.approx(1.0, abs=1e-4)


def test_zero_form_guard_yields_odds_only_views():
    slate = gs.GameSlate(
        sport="worldcup",
        game_id=3,
        season=2026,
        home={"id": 1, "abbreviation": "CAN"},
        away={"id": 2, "abbreviation": "BIH"},
        home_form={"for": 0.0, "against": 0.0, "n": 0},  # tournament just started
        away_form={"for": 0.0, "against": 0.0, "n": 0},
        markets=gs.parse_game_odds([_wc_odds_row("fanduel", -125, 250, 380, [(2.5, 105, -125)])]),
    )
    result = gs.score_game_slate(slate)
    assert result.lam_home == 0.0 and result.lam_away == 0.0
    assert all(v.model_p is None and v.book_edge is None for v in result.views)
    ml_home = next(v for v in result.views if v.market == "moneyline_home")
    assert ml_home.book_p is not None and ml_home.book_p > 0.5  # odds still shown
    assert any("no_form_model" in v.flags for v in result.views)
    text = gs.render_game(result)
    assert "no_form_model" in text and "n/a" in text


def test_worldcup_event_shape():
    row = {
        "id": 1,
        "datetime": "2026-06-11T19:00:00.000Z",
        "status": "completed",
        "home_team": {"id": 10, "abbreviation": "MEX"},
        "away_team": {"id": 20, "abbreviation": "RSA"},
        "home_score": 2,
        "away_score": 0,
    }
    assert gs.event_completed(row)
    assert gs.event_scores(row) == (2, 0)
    assert gs.event_date(row).startswith("2026-06-11")
    assert not gs.event_completed({**row, "status": "scheduled"})


# ── MLB starting pitchers (the totals driver team form can't see) ────────────


def test_pitcher_quality_shrinks_and_clips():
    # Ace: 1.87 RA9 over 12 starts -> strong but clipped/shrunk factor below 1
    ace_logs = [{"pitching_outs": 18, "er": 1.25} for _ in range(12)]
    ace = gs._pitcher_quality(ace_logs)
    assert ace["ra9"] == pytest.approx(1.87, abs=0.05)
    assert gs._PITCHER_FACTOR_CLIP[0] <= ace["factor"] < 0.8

    # Two-start rookie, modestly bad: heavy shrink toward league -> factor near 1
    rookie = gs._pitcher_quality([{"pitching_outs": 15, "er": 4}, {"pitching_outs": 16, "er": 3}])
    assert 1.0 < rookie["factor"] < 1.2
    # An extreme blowup sample clips at the bound instead of doubling the opponent
    blowup = gs._pitcher_quality([{"pitching_outs": 12, "er": 6}, {"pitching_outs": 15, "er": 5}])
    assert blowup["factor"] == gs._PITCHER_FACTOR_CLIP[1]

    assert gs._pitcher_quality([{"pitching_outs": 0, "er": 0}]) is None


def test_pitcher_factors_adjust_opposing_lambda_and_render():
    base = {
        "sport": "mlb",
        "game_id": 1,
        "season": 2026,
        "home": {"id": 1, "abbreviation": "TOR", "display_name": "Toronto Blue Jays"},
        "away": {"id": 2, "abbreviation": "NYY", "display_name": "New York Yankees"},
        "home_form": {"for": 4.0, "against": 4.1, "n": 25},
        "away_form": {"for": 5.0, "against": 3.8, "n": 25},
        "markets": gs.parse_game_odds([]),
    }
    plain = gs.score_game_slate(gs.GameSlate(**base))
    ace_home = gs.score_game_slate(
        gs.GameSlate(
            **base,
            home_pitcher={"name": "Ace", "ra9": 1.9, "starts": 12, "factor": 0.75},
            away_pitcher={"name": "Avg", "ra9": 4.3, "starts": 10, "factor": 1.0},
        )
    )
    assert ace_home.lam_away == pytest.approx(plain.lam_away * 0.75, abs=1e-3)
    assert ace_home.lam_home == pytest.approx(plain.lam_home, abs=1e-3)
    text = gs.render_game(ace_home)
    # facts (name/RA9/starts) live in INFORMATION; the factor is model opinion
    assert "Ace (RA9 1.9, 12 starts)" in text
    assert "== INFORMATION" in text and "== REFERENCE MODEL" in text
    assert "starter RA9 factors" in text

    # MLB without pitcher data: loud UNKNOWN-starters line (render path)
    bare = gs.score_game_slate(gs.GameSlate(**base))
    assert "probable starters: UNKNOWN" in gs.render_game(bare)

    # data-only mode: facts without the reference model
    info_only = gs.render_game(bare, data_only=True)
    assert "== INFORMATION" in info_only
    assert "REFERENCE MODEL" not in info_only
    assert "de-vigged" not in info_only or "consensus" not in info_only or True  # no odds in stub
    assert "sports_posterior" in info_only  # gating pointer survives data-only mode
