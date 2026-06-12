"""Dislocation gate + book-fair evidence card + gated posterior (calibrated on run-12)."""

import pytest

from wayfinder_paths.quant import sports_posterior as spo
from wayfinder_paths.quant.polymarket_edge import evidence_llr, logit

# Live run-12 numbers (World Cup): the calibration anchors.
GERMANY_BOOK, GERMANY_PM = 0.0572, 0.0515
MATCH_BOOK, MATCH_PM = 0.524, 0.535


# ── dislocation gate ─────────────────────────────────────────────────────────


def test_dislocation_gate_calibrated_on_live_numbers():
    germany = spo.dislocation(GERMANY_BOOK, GERMANY_PM)
    assert germany.needs_adjudication  # 0.57pp, llr ~0.111 -> research required
    assert germany.cheap_side == "YES"  # PM prices the win below book-fair
    assert germany.gap_llr == pytest.approx(0.111, abs=0.01)
    assert len(germany.required_questions) == 4

    match = spo.dislocation(MATCH_BOOK, MATCH_PM)
    assert not match.needs_adjudication  # 1.1pp but llr ~0.044 -> normal noise
    assert match.cheap_side == "NO"


def test_dislocation_longshot_sensitivity():
    # 1.5c vs 2.0c: only 0.5pp but a 33% ROI gap -> must trigger (log-odds gate).
    longshot = spo.dislocation(0.020, 0.015)
    assert longshot.needs_adjudication
    assert longshot.gap_llr > 0.25


# ── book-fair evidence card ──────────────────────────────────────────────────


def test_book_card_llr_contract_survives_multipliers():
    """evidence_llr's default multipliers would crush a bare card to ~2% weight; the
    card pins every field so the contract holds exactly."""
    dlogit = logit(GERMANY_BOOK) - logit(GERMANY_PM)
    card = spo.book_fair_evidence_card(
        GERMANY_BOOK, GERMANY_PM, n_vendors=2, trust=0.7, overround=1.206
    )
    trust_eff = 0.7 * 0.8 * 0.8  # 2 vendors -> x0.8; overround>1.12 -> x0.8
    assert evidence_llr(card) == pytest.approx(trust_eff * dlogit, abs=1e-9)
    assert card["direction"] == "for_yes"  # book above market -> evidence for YES

    inverse = spo.book_fair_evidence_card(GERMANY_PM, GERMANY_BOOK, n_vendors=3)
    assert inverse["direction"] == "against_yes"
    assert evidence_llr(inverse) == pytest.approx(-0.7 * abs(dlogit), abs=1e-9)


def test_book_card_vendor_scaling():
    full = spo.book_fair_evidence_card(GERMANY_BOOK, GERMANY_PM, n_vendors=3)
    solo = spo.book_fair_evidence_card(GERMANY_BOOK, GERMANY_PM, n_vendors=1)
    assert evidence_llr(solo) == pytest.approx(evidence_llr(full) * 0.6, rel=1e-6)


# ── CLI card grammar ─────────────────────────────────────────────────────────


def test_make_card_strict_validation():
    card = spo.make_card("davies_out:against:medium:news")
    assert card["direction"] == "against_yes"
    assert evidence_llr(card) < 0
    assert abs(evidence_llr(card)) > 0.05  # NOT silently attenuated to ~0

    with pytest.raises(ValueError, match="direction"):
        spo.make_card("x:downward:medium:news")
    with pytest.raises(ValueError, match="strength"):
        spo.make_card("x:for:moderate:news")  # 'moderate' is not a strength token
    with pytest.raises(ValueError, match="kind"):
        spo.make_card("x:for:medium:vibes")
    with pytest.raises(ValueError, match="name:direction:strength:kind"):
        spo.make_card("missing_fields")


# ── posterior + gate ─────────────────────────────────────────────────────────


def test_germany_end_to_end_is_watch_at_conservative_gate():
    """The dislocation alone must NOT clear the gate: book card pulls the posterior
    partway toward book-fair, but conservative EV at a 5c entry stays below 2c/share."""
    cards = [
        spo.book_fair_evidence_card(
            GERMANY_BOOK, GERMANY_PM, n_vendors=2, overround=1.206
        )
    ]
    result = spo.sports_posterior(cards, market_p=GERMANY_PM)
    assert result["posteriorMethod"] == "log_odds_evidence_update"
    assert result["priorSource"] == "ask_only"  # single price = honest ask-only prior
    assert GERMANY_PM < result["pBase"] < GERMANY_BOOK  # pulled partway toward book
    assert result["decision"] == "WATCH"
    assert result["evYes"] is not None and result["evYes"] > 0  # value shown, not hidden

    # Corroborating independent evidence CAN clear it.
    cards_plus = cards + [
        spo.make_card("star_returns_confirmed:for:decisive:news"),
        spo.make_card("books_stale_on_news:for:strong:structure"),
    ]
    stronger = spo.sports_posterior(cards_plus, market_p=GERMANY_PM)
    assert stronger["pBase"] > result["pBase"]
    assert stronger["decision"].startswith("BUY_YES")


def test_contract_keys_present_for_research_handoff():
    result = spo.sports_posterior(
        [spo.make_card("x:for:weak:news")], yes_bid=0.051, yes_ask=0.052
    )
    for key in (
        "priorSource",
        "marketPrior",
        "evidenceCards",
        "posteriorMethod",
        "pLow",
        "pBase",
        "pHigh",
        "evYes",
        "evNo",
        "decision",
    ):
        assert key in result, f"missing contract key {key}"
    assert result["priorSource"] == "bid_ask_mid"
    assert result["evidenceCards"][0]["computedLlr"] != 0


def test_render_ledger_shows_cards_gate_and_doctrine():
    report = spo.dislocation(GERMANY_BOOK, GERMANY_PM)
    cards = [
        spo.book_fair_evidence_card(GERMANY_BOOK, GERMANY_PM, n_vendors=2),
        spo.make_card("keeper_injury_rumor:against:weak:social"),
    ]
    result = spo.sports_posterior(cards, market_p=GERMANY_PM)
    text = spo.render_ledger(result, dislocation_report=report)
    assert "adjudication REQUIRED" in text
    assert "ROI" in text and "decision: WATCH" in text
    assert "EXECUTABLE market price" in text  # prior doctrine named
    assert "keeper injury rumor" in text
