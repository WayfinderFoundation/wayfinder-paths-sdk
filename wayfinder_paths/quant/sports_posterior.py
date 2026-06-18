"""Posterior formation for sports bets: dislocation detection + evidence-ledger blending.

The slates (prop/game/futures) produce de-vigged SPORTSBOOK probabilities; Polymarket is
the executable venue. When the two disagree, the worst move is picking the cheap side on
trust — the gap may be a structural discount (resolution rules, capital lockup, flow) or
real information one venue hasn't absorbed. This module makes the disciplined path the
easy path:

- ``dislocation()`` decides when a book-vs-market gap is big enough to REQUIRE a research
  pass ("why does the cheap side exist?") before any recommendation.
- ``book_fair_evidence_card()`` folds the sportsbook number into the posterior as ONE
  capped evidence card over the **executable market prior** (platform doctrine: the
  order book is the prior; everything else is evidence).
- ``sports_posterior()`` runs the same capped log-odds machinery the research agent uses
  for crypto (:mod:`wayfinder_paths.quant.polymarket_edge`) and gates the decision on the
  conservative band — the dislocation alone never clears the gate; corroborating evidence
  must.
- ``render_ledger()`` prints the per-card ledger so the final answer SHOWS what moved the
  probability and which prior was trusted.

CLI:
    poetry run python -m wayfinder_paths.quant.sports_posterior \
        --market 0.0515 --book 0.0572 --vendors 2 --overround 1.206 \
        --card "davies_out:against:medium:news"
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from wayfinder_paths.quant.polymarket_edge import (
    bayes_update_from_evidence,
    binary_no_ev,
    binary_yes_ev,
    evidence_llr,
    implied_prior_from_quote,
    inv_logit,
    logit,
    posterior_band_from_evidence,
    reprice_forecast_from_quote,
)
from wayfinder_paths.quant.sports_props import market_edge

# evidence_llr multiplies the stored llr by quality multipliers; with the pinned fields
# below the residual product is sourceQuality("market_data") = 0.9. The card divides by
# this so evidence_llr(card) == ±trust_eff * |Δlogit| exactly.
_BOOK_CARD_RESIDUAL = 0.9

REQUIRED_QUESTIONS = (
    "Is there injury/availability/lineup news AFTER the sportsbook lines were set that "
    "the cheap venue has absorbed and the books have not (or vice versa)?",
    "Do the Polymarket resolution rules match the sportsbook settlement exactly "
    "(e.g. an 'Other'/field bundle, voids, OT/shootout treatment)?",
    "Is the discount structural rather than informational — capital lockup to "
    "resolution, fees, one-sided flow, or thin depth at the quoted price?",
    "How sensitive is the book-fair number to the de-vig method (few vendors, fat "
    "overround shading longshots)?",
)

# CLI card grammar -> evidence-card multiplier bundles. The failure mode of a sloppy
# card is SILENT attenuation (defaults multiply to ~0.018), so every kind pins all five
# multiplier fields explicitly.
_CARD_KINDS: dict[str, dict[str, str]] = {
    "news": {
        "sourceQuality": "reputable_secondary",
        "freshness": "fresh",
        "independence": "independent",
        "alreadyPriced": "maybe",
        "resolutionRelevance": "direct",
    },
    "data": {
        "sourceQuality": "primary",
        "freshness": "fresh",
        "independence": "partially_overlapping",  # models share inputs with the books
        "alreadyPriced": "maybe",
        "resolutionRelevance": "direct",
    },
    "structure": {  # resolution rules / lockup / flow — market-structure evidence
        "sourceQuality": "market_data",
        "freshness": "fresh",
        "independence": "independent",
        "alreadyPriced": "unlikely",
        "resolutionRelevance": "direct",
    },
    "social": {
        "sourceQuality": "social",
        "freshness": "fresh",
        "independence": "partially_overlapping",
        "alreadyPriced": "maybe",
        "resolutionRelevance": "direct",
    },
}
_DIRECTIONS = {"for": "for_yes", "against": "against_yes"}
_STRENGTHS = ("weak", "medium", "strong", "decisive")


# ── dislocation detection ────────────────────────────────────────────────────


@dataclass
class DislocationReport:
    book_fair_p: float
    market_p: float
    gap_pp: float  # |book - market| in probability points
    gap_rel: float  # display only (denominator = market_p)
    gap_llr: float  # |logit(book) - logit(market)| — the gate
    cheap_side: str  # "YES" if the market prices the outcome below book-fair
    needs_adjudication: bool
    required_questions: tuple[str, ...] = field(default=REQUIRED_QUESTIONS)


def dislocation(
    book_fair_p: float,
    market_p: float,
    *,
    min_abs_pp: float = 0.005,
    min_abs_llr: float = 0.08,
) -> DislocationReport:
    """Is the book-vs-executable gap big enough to require adjudication?

    The gate is a LOG-ODDS gap (relative-percent gates are denominator-ambiguous and
    underweight longshots: 1.5c vs 2.0c is only 0.5pp but a 33% ROI difference).
    Calibration: Germany WC winner 5.15c vs 5.72% book (Δlogit 0.111) triggers; a
    52.4% vs 53.5% match moneyline (Δlogit 0.044) does not.
    """
    book = float(book_fair_p)
    market = float(market_p)
    gap_pp = abs(book - market)
    gap_llr = abs(logit(book) - logit(market))
    return DislocationReport(
        book_fair_p=book,
        market_p=market,
        gap_pp=gap_pp,
        gap_rel=gap_pp / market if market > 0 else 0.0,
        gap_llr=gap_llr,
        cheap_side="YES" if market < book else "NO",
        needs_adjudication=gap_pp >= min_abs_pp and gap_llr >= min_abs_llr,
    )


# ── evidence cards ───────────────────────────────────────────────────────────


def book_fair_evidence_card(
    book_fair_p: float,
    market_p: float,
    *,
    n_vendors: int,
    trust: float = 0.7,
    overround: float | None = None,
) -> dict[str, Any]:
    """The de-vigged sportsbook number as ONE capped evidence card over the market prior.

    Contract: ``evidence_llr(card) == ±trust_eff * |Δlogit|`` where trust_eff scales the
    base trust by vendor coverage (>=3 -> 1.0, 2 -> 0.8, 1 -> 0.6) and takes a haircut
    when the field's overround is fat (> 1.12 -> x0.8; proportional de-vig overstates
    longshots there). All quality multipliers are pinned — the defaults would silently
    attenuate the card to ~2% of its intended weight.
    """
    vendors = int(n_vendors)
    vendor_scale = 1.0 if vendors >= 3 else 0.8 if vendors == 2 else 0.6
    over_scale = 0.8 if (overround is not None and float(overround) > 1.12) else 1.0
    trust_eff = float(trust) * vendor_scale * over_scale
    dlogit = logit(float(book_fair_p)) - logit(float(market_p))
    detail = f"{vendors} vendor(s)"
    if overround is not None:
        detail += f", overround {float(overround):.3f}"
    return {
        "claim": (
            f"De-vigged sportsbook consensus prices this at {float(book_fair_p):.4f} "
            f"vs the market's {float(market_p):.4f} ({detail})."
        ),
        "direction": "for_yes" if dlogit > 0 else "against_yes",
        "llr": trust_eff * abs(dlogit) / _BOOK_CARD_RESIDUAL,
        "sourceQuality": "market_data",
        "freshness": "fresh",
        "independence": "independent",
        "alreadyPriced": "unlikely",
        "resolutionRelevance": "direct",
        "rationale": (
            f"book-vs-market log-odds gap {dlogit:+.3f} x effective trust "
            f"{trust_eff:.2f} (base {float(trust):.2f}, vendor x{vendor_scale}, "
            f"overround x{over_scale})"
        ),
        "kind": "book_fair",
    }


def make_card(spec: str) -> dict[str, Any]:
    """Build a full evidence card from CLI grammar ``name:direction:strength:kind``.

    Strict: an invalid token raises (the alternative — evidence_llr's silent 0.0 on an
    unknown direction, or default multipliers crushing the weight — loses the evidence
    without telling anyone).
    """
    parts = [p.strip() for p in str(spec).split(":")]
    if len(parts) != 4:
        raise ValueError(
            f"card {spec!r} must be name:direction:strength:kind "
            f"(direction {sorted(_DIRECTIONS)}, strength {list(_STRENGTHS)}, "
            f"kind {sorted(_CARD_KINDS)})"
        )
    name, direction, strength, kind = parts
    if direction not in _DIRECTIONS:
        raise ValueError(f"direction {direction!r} must be one of {sorted(_DIRECTIONS)}")
    if strength not in _STRENGTHS:
        raise ValueError(f"strength {strength!r} must be one of {list(_STRENGTHS)}")
    if kind not in _CARD_KINDS:
        raise ValueError(f"kind {kind!r} must be one of {sorted(_CARD_KINDS)}")
    return {
        "claim": name.replace("_", " "),
        "direction": _DIRECTIONS[direction],
        "strength": strength,
        "kind": kind,
        **_CARD_KINDS[kind],
    }


# ── posterior ────────────────────────────────────────────────────────────────


def sports_posterior(
    cards: list[dict[str, Any]],
    *,
    market_p: float | None = None,
    yes_bid: float | None = None,
    yes_ask: float | None = None,
    max_abs_log_odds_move: float = 0.75,
    min_ev: float = 0.02,
) -> dict[str, Any]:
    """Capped log-odds posterior over the executable market prior, conservatively gated.

    Output keys follow the research forecast contract (priorSource/marketPrior/
    evidenceCards/posteriorMethod/pLow/pBase/pHigh/evYes/evNo/decision) so a delegating
    agent can paste the result straight into its findings.
    """
    if yes_bid is None and yes_ask is None:
        if market_p is None:
            raise ValueError("need yes_bid/yes_ask or market_p")
        yes_ask = float(market_p)  # honest: a single price is an ask-only prior
    prior_info = implied_prior_from_quote(yes_bid=yes_bid, yes_ask=yes_ask)
    prior = float(prior_info["marketPrior"])

    update = bayes_update_from_evidence(
        prior, cards, max_abs_log_odds_move=max_abs_log_odds_move
    )
    band = posterior_band_from_evidence(
        prior, cards, max_abs_log_odds_move=max_abs_log_odds_move
    )
    reprice = reprice_forecast_from_quote(
        p_low=band["pLow"],
        p_base=band["pBase"],
        p_high=band["pHigh"],
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        min_ev=min_ev,
    )
    entry_yes = reprice.get("entryYes")
    entry_no = reprice.get("entryNo")
    p_base = float(band["pBase"])
    sizing = (
        market_edge(p_base, float(entry_yes)) if entry_yes is not None else None
    )
    return {
        "priorSource": prior_info["priorSource"],
        "marketPrior": prior,
        "evidenceCards": update["evidenceCards"],
        "rawLogOddsMove": update["rawLogOddsMove"],
        "cappedLogOddsMove": update["cappedLogOddsMove"],
        "posteriorMethod": "log_odds_evidence_update",
        "pLow": float(band["pLow"]),
        "pBase": p_base,
        "pHigh": float(band["pHigh"]),
        "entryYes": entry_yes,
        "entryNo": entry_no,
        "evYes": binary_yes_ev(p_base, entry_yes) if entry_yes is not None else None,
        "evNo": binary_no_ev(p_base, entry_no) if entry_no is not None else None,
        "yesGate": reprice["yesGate"],
        "noGate": reprice["noGate"],
        "decision": reprice["decision"],
        "kelly": sizing.kelly if sizing is not None else None,
        "minEv": float(min_ev),
    }


def model_fair_evidence_card(
    model_p: float,
    market_p: float,
    *,
    model_type: str,
    trust: float = 0.55,
    sample_size: int | None = None,
    calibrated: bool = False,
    oos_validated: bool = False,
    thin_sample: bool = False,
    market_implied: bool = False,
    diagnostic_only: bool = False,
    uncertainty_pp: float | None = None,
) -> dict[str, Any]:
    """Represent a sports model probability as capped evidence over the executable prior."""

    trust_eff = float(trust)
    if calibrated:
        trust_eff *= 1.1
    if oos_validated:
        trust_eff *= 1.1
    if thin_sample:
        trust_eff *= 0.65
    if market_implied:
        trust_eff *= 0.50
    if diagnostic_only:
        trust_eff *= 0.0
    if uncertainty_pp is not None and float(uncertainty_pp) > 8:
        trust_eff *= 0.75
    if sample_size is not None and int(sample_size) < 50:
        trust_eff *= 0.75

    dlogit = logit(float(model_p)) - logit(float(market_p))
    return {
        "claim": f"{model_type} model prices this at {float(model_p):.4f} vs market {float(market_p):.4f}.",
        "direction": "for_yes" if dlogit > 0 else "against_yes",
        "llr": trust_eff * abs(dlogit) / _BOOK_CARD_RESIDUAL,
        "sourceQuality": "market_data",
        "freshness": "fresh",
        "independence": "partially_overlapping",
        "alreadyPriced": "maybe",
        "resolutionRelevance": "direct",
        "rationale": (
            f"model-vs-market log-odds gap {dlogit:+.3f} x effective trust "
            f"{trust_eff:.2f}"
        ),
        "kind": "sports_model",
        "modelType": model_type,
        "diagnosticOnly": diagnostic_only,
    }


def simulation_evidence_card(
    sim_p: float,
    market_p: float,
    *,
    sim_type: str = "monte_carlo",
    n_sims: int | None = None,
    path_assumption: str | None = None,
    rating_source: str | None = None,
    trust: float = 0.6,
    diagnostic_only: bool = False,
    approx_bracket: bool = False,
) -> dict[str, Any]:
    """Represent Monte Carlo/path simulation output as capped evidence."""

    trust_eff = float(trust)
    if n_sims is not None and int(n_sims) < 5000:
        trust_eff *= 0.75
    if approx_bracket:
        trust_eff *= 0.65
    if diagnostic_only:
        trust_eff *= 0.0
    dlogit = logit(float(sim_p)) - logit(float(market_p))
    return {
        "claim": f"{sim_type} simulation prices this at {float(sim_p):.4f} vs market {float(market_p):.4f}.",
        "direction": "for_yes" if dlogit > 0 else "against_yes",
        "llr": trust_eff * abs(dlogit) / _BOOK_CARD_RESIDUAL,
        "sourceQuality": "market_data",
        "freshness": "fresh",
        "independence": "partially_overlapping",
        "alreadyPriced": "maybe",
        "resolutionRelevance": "direct",
        "rationale": (
            f"simulation log-odds gap {dlogit:+.3f} x effective trust {trust_eff:.2f}; "
            f"path={path_assumption or 'unspecified'}, ratings={rating_source or 'unspecified'}"
        ),
        "kind": "sports_simulation",
        "simType": sim_type,
        "diagnosticOnly": diagnostic_only,
        "approxBracket": approx_bracket,
    }


def posterior_from_packs(
    *,
    surface_pack: dict[str, Any],
    analysis_pack: dict[str, Any],
    context_pack: dict[str, Any] | None = None,
    min_ev: float = 0.02,
) -> dict[str, Any]:
    """Build a decisionPack from sports surface/model/context WorkPacks."""

    surface_rows = (surface_pack.get("payload") or {}).get("markets") or []
    by_key: dict[str, dict[str, Any]] = {}
    for row in surface_rows:
        key = str(
            row.get("participantId")
            or row.get("participant_id")
            or row.get("marketId")
            or row.get("id")
            or row.get("name")
        )
        by_key[key] = row

    context_cards = []
    if context_pack:
        context_cards = (context_pack.get("payload") or {}).get("evidenceCards") or []

    decisions = []
    for model_row in (analysis_pack.get("payload") or {}).get("rows") or []:
        key = str(
            model_row.get("participant_id")
            or model_row.get("participantId")
            or model_row.get("marketId")
            or model_row.get("id")
            or model_row.get("name")
        )
        surface = by_key.get(key, {})
        ask = surface.get("ask") or surface.get("entryPrice") or model_row.get("entryPrice")
        bid = surface.get("bid")
        market_p = (
            (float(bid) + float(ask)) / 2.0
            if bid is not None and ask is not None
            else float(ask) if ask is not None
            else surface.get("marketPrior")
        )
        model_p = (
            model_row.get("modelP")
            or model_row.get("probability")
            or model_row.get("pBase")
        )
        if market_p is None or model_p is None:
            decisions.append(
                {
                    **model_row,
                    "decision": "WATCH",
                    "skipReason": "missing_market_or_model_probability",
                }
            )
            continue
        cards = [
            model_fair_evidence_card(
                float(model_p),
                float(market_p),
                model_type=str((analysis_pack.get("payload") or {}).get("recipeId") or "sports_model"),
                uncertainty_pp=(
                    abs(float(model_row["pHigh"]) - float(model_row["pLow"])) * 100
                    if model_row.get("pHigh") is not None and model_row.get("pLow") is not None
                    else None
                ),
            )
        ]
        cards.extend(context_cards)
        posterior = sports_posterior(
            cards,
            market_p=float(market_p),
            yes_bid=float(bid) if bid is not None else None,
            yes_ask=float(ask) if ask is not None else None,
            min_ev=min_ev,
        )
        decisions.append(
            {
                **model_row,
                "venue": surface.get("venue"),
                "marketPrior": posterior["marketPrior"],
                "entryPrice": posterior.get("entryYes"),
                "pLow": posterior["pLow"],
                "pBase": posterior["pBase"],
                "pHigh": posterior["pHigh"],
                "evYes": posterior["evYes"],
                "evNo": posterior["evNo"],
                "decision": posterior["decision"],
                "posteriorLedger": posterior,
            }
        )

    return {
        "packType": "decisionPack",
        "domain": "sports",
        "intent": "sports_decision",
        "stage": "decision",
        "schemaVersion": "1.0",
        "inputPacks": [
            surface_pack.get("packId"),
            analysis_pack.get("packId"),
            *( [context_pack.get("packId")] if context_pack else [] ),
        ],
        "summary": "Sports posterior decisions from WorkPacks.",
        "payload": {"rows": decisions},
        "reusePolicy": {
            "canReuseFor": ["final_answer"],
            "mustRehydrateBefore": ["execute", "place_order", "recommend_buy"],
            "ttlSeconds": 60,
        },
        "sensitivity": "public",
    }


# ── render ───────────────────────────────────────────────────────────────────


def render_ledger(
    result: dict[str, Any], *, dislocation_report: DislocationReport | None = None
) -> str:
    prior = float(result["marketPrior"])
    lines = [
        f"POSTERIOR LEDGER — prior {prior:.4f} ({result['priorSource']}), "
        f"method {result['posteriorMethod']}",
    ]
    if dislocation_report is not None:
        d = dislocation_report
        verdict = (
            "adjudication REQUIRED"
            if d.needs_adjudication
            else "below adjudication threshold: treat as VENUE NOISE, not edge"
        )
        lines.append(
            f"dislocation: book {d.book_fair_p:.4f} vs market {d.market_p:.4f} "
            f"(gap {d.gap_pp * 100:.2f}pp, llr {d.gap_llr:.3f}, cheap side {d.cheap_side}) "
            f"-> {verdict}"
        )
    lines.append("")
    lines.append(f"{'evidence':<44} {'dir':<8} {'llr':>7}  {'Δpp':>6}")
    for card in result["evidenceCards"]:
        llr = float(card.get("computedLlr", evidence_llr(card)))
        delta_pp = (inv_logit(logit(prior) + llr) - prior) * 100
        claim = str(card.get("claim", "?"))[:43]
        direction = "for" if card.get("direction") == "for_yes" else "against"
        lines.append(f"{claim:<44} {direction:<8} {llr:>+7.3f}  {delta_pp:>+6.2f}")
    raw, capped = float(result["rawLogOddsMove"]), float(result["cappedLogOddsMove"])
    move = f"total log-odds move: {capped:+.3f}"
    if abs(raw - capped) > 1e-9:
        move += f" (raw {raw:+.3f}, CAP BINDS)"
    lines.append(move)
    lines.append(
        f"posterior: {prior:.4f} -> pBase {result['pBase']:.4f} "
        f"[pLow {result['pLow']:.4f}, pHigh {result['pHigh']:.4f}]"
    )
    for side, entry, ev in (
        ("YES", result.get("entryYes"), result.get("evYes")),
        ("NO", result.get("entryNo"), result.get("evNo")),
    ):
        if entry is not None and ev is not None:
            lines.append(
                f"{side}: entry {float(entry):.4f}, EV {float(ev):+.4f}/share "
                f"(ROI {float(ev) / float(entry) * 100:+.1f}%)"
            )
    gate_bits = []
    for name, gate in (("yes", result.get("yesGate")), ("no", result.get("noGate"))):
        if gate:
            gate_bits.append(
                f"{name}: conservativeEV {float(gate['conservativeEv']):+.4f} "
                f"{'PASS' if gate['passes'] else 'fail'}"
            )
    lines.append(
        f"decision: {result['decision']} (min_ev {result['minEv']:.3f}; "
        + "; ".join(gate_bits)
        + ")"
    )
    if result.get("kelly") is not None:
        lines.append(f"kelly (fractional, on pBase vs entryYes): {result['kelly']:.4f}")
    lines.append("")
    lines.append(
        "NOTE: the prior is the EXECUTABLE market price; the de-vigged sportsbook "
        "number enters only as a capped evidence card. A dislocation alone should not "
        "clear the gate — adjudicate the cheap side (research) before recommending."
    )
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────────


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Blend the executable market prior with evidence cards into a "
        "gated posterior (sports dislocation adjudication)."
    )
    parser.add_argument("--label", default=None, help="market label echoed in the header")
    parser.add_argument("--market", type=float, default=None, help="single market price")
    parser.add_argument("--bid", type=float, default=None, help="executable YES bid")
    parser.add_argument("--ask", type=float, default=None, help="executable YES ask")
    parser.add_argument("--book", type=float, default=None, help="de-vigged book-fair p")
    parser.add_argument("--vendors", type=int, default=2)
    parser.add_argument("--overround", type=float, default=None)
    parser.add_argument("--trust", type=float, default=0.7)
    parser.add_argument("--min-ev", type=float, default=0.02)
    parser.add_argument(
        "--card",
        action="append",
        default=[],
        help="name:direction:strength:kind (repeatable); "
        "direction for|against, strength weak|medium|strong|decisive, "
        "kind news|data|structure|social",
    )
    parser.add_argument(
        "--card-json",
        action="append",
        default=[],
        help="full evidence-card JSON (escape hatch for research-built cards)",
    )
    args = parser.parse_args()

    market_ref = (
        args.market
        if args.market is not None
        else ((args.bid + args.ask) / 2 if args.bid and args.ask else args.ask or args.bid)
    )
    report = None
    cards: list[dict[str, Any]] = []
    if args.book is not None and market_ref is not None:
        report = dislocation(args.book, market_ref)
        cards.append(
            book_fair_evidence_card(
                args.book,
                market_ref,
                n_vendors=args.vendors,
                trust=args.trust,
                overround=args.overround,
            )
        )
    cards.extend(make_card(spec) for spec in args.card)
    cards.extend(json.loads(blob) for blob in args.card_json)

    result = sports_posterior(
        cards,
        market_p=args.market,
        yes_bid=args.bid,
        yes_ask=args.ask,
        min_ev=args.min_ev,
    )
    if args.label:
        print(f"=== {args.label} ===")
    print(render_ledger(result, dislocation_report=report))


if __name__ == "__main__":
    _main()
