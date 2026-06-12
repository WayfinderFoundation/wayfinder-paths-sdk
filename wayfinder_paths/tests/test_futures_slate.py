"""Futures field de-vig: per-vendor normalization, vendor medians, market scoping."""

import pytest

from wayfinder_paths.quant import futures_slate as fs


def _row(vendor, name, american, *, market_type="outright", market_name="World Cup Winner", dec=None):
    return {
        "market_type": market_type,
        "market_name": market_name,
        "vendor": vendor,
        "american_odds": american,
        "decimal_odds": dec,
        "subject": {"name": name, "abbreviation": name[:3].upper()},
    }


def test_field_devig_normalizes_per_vendor_and_medians():
    # A realistic (complete) field: per-vendor implied probs sum past 1 — that excess
    # IS the vig the normalization removes.
    rows = [
        _row("fanduel", "Spain", 120),
        _row("fanduel", "France", 200),
        _row("fanduel", "Brazil", 350),
        _row("fanduel", "Canada", 900),
        _row("fanduel", "Haiti", 9000),
        _row("draftkings", "Spain", 110),
        _row("draftkings", "France", 210),
        _row("draftkings", "Brazil", 330),
        _row("draftkings", "Canada", 1000),
        _row("draftkings", "Haiti", 12000),
    ]
    result = fs.score_futures(rows, market_type="outright")
    assert result.overround > 1.0  # the field carries vig
    assert sum(o.fair_p for o in result.outcomes) == pytest.approx(1.0)
    assert result.outcomes[0].subject == "Spain"  # favorite first
    haiti = next(o for o in result.outcomes if o.subject == "Haiti")
    assert haiti.fair_p < 0.02
    assert haiti.best_american == 12000 and haiti.best_vendor == "draftkings"
    assert all(o.n_vendors == 2 for o in result.outcomes)
    # the raw quote includes vig: fair must be below raw for every outcome
    assert all(o.fair_p < o.raw_implied_p for o in result.outcomes)


def test_multi_market_type_requires_market_name():
    rows = [
        _row("fanduel", "Spain", -300, market_type="group_winner", market_name="Group A"),
        _row("fanduel", "Jordan", 800, market_type="group_winner", market_name="Group J"),
    ]
    with pytest.raises(ValueError, match="market_name"):
        fs.score_futures(rows, market_type="group_winner")
    one = fs.score_futures(rows, market_type="group_winner", market_name="Group A")
    assert len(one.outcomes) == 1 and one.outcomes[0].fair_p == pytest.approx(1.0)


def test_render_includes_two_stage_note_and_overround():
    rows = [
        _row("fanduel", "Spain", 450),
        _row("fanduel", "France", 600),
    ]
    result = fs.score_futures(rows, market_type="outright")
    result.sport = "worldcup"
    text = fs.render_futures(result)
    assert "market_edge" in text and "Polymarket" in text
    assert "overround" in text
