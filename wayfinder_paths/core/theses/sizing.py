"""Construction arithmetic, never asset selection or expected-return optimization."""

from fractions import Fraction
from itertools import combinations

from wayfinder_paths.core.theses.models import Construction, Variant


def construction_errors(
    variant: Variant,
    construction: Construction | None = None,
    *,
    intent: str | None = None,
) -> list[str]:
    if not variant.positions:
        return []
    errors = []
    if construction is not None:
        if intent is not None and intent != construction.intent:
            errors.append("Proposal intent conflicts with the frozen construction")
        intent = construction.intent
    if intent != "relative":
        return errors
    legs = variant.positions
    longs = sum(p.capital_bps * p.leverage for p in legs if p.direction == "long")
    shorts = sum(p.capital_bps * p.leverage for p in legs if p.direction == "short")
    if (
        any(p.kind == "prediction" for p in legs)
        or min(longs, shorts) <= 0
        or abs(longs - shorts) > 2
    ):
        errors.append(
            f"{variant.budget_usd}: relative portfolios require matched long/short dollar notionals, "
            "not equal collateral; size the existing legs with research_quantify_portfolio(construction=...). "
            "The payoff is N*(thesis return - benchmark return), not portfolio return minus benchmark return"
        )
    if construction is not None:
        benchmark = [
            p for p in legs if p.instrument_id == construction.benchmark_instrument_id
        ]
        if not benchmark or benchmark[0].direction != construction.benchmark_direction:
            errors.append(
                f"{variant.budget_usd}: retain the resolved benchmark leg and its frozen direction"
            )
        if any(
            p.direction == construction.benchmark_direction
            for p in legs
            if p.instrument_id != construction.benchmark_instrument_id
        ):
            errors.append(
                f"{variant.budget_usd}: non-benchmark legs must oppose the benchmark direction"
            )
    return errors


def size_variant(variant: Variant, construction: Construction) -> Variant:
    """Preserve instruments/leverage and within-side notional preferences.

    Match both sides, then round to integer capital bps under the existing 2-bps
    notional tolerance. At most 12 legs means at most 924 floor/ceiling choices.
    No asset removal, invented hedge, cash padding or leverage adjustment.
    """
    if construction.mode != "matched_relative" or not variant.positions:
        return variant
    legs = variant.positions
    benchmark = next(
        (p for p in legs if p.instrument_id == construction.benchmark_instrument_id),
        None,
    )
    if (
        benchmark is None
        or benchmark.direction != construction.benchmark_direction
        or any(p.kind == "prediction" for p in legs)
        or any(p.direction == benchmark.direction for p in legs if p is not benchmark)
        or len(legs) < 2
    ):
        raise ValueError(
            "Supply the resolved benchmark leg and opposing thesis legs; sizing cannot invent them"
        )
    if variant.cash_bps:
        raise ValueError("Sizing requires a full target, not cash or padded collateral")
    leverage = [Fraction(str(p.leverage)) for p in legs]
    totals = {
        side: sum(
            (
                p.capital_bps * lev
                for p, lev in zip(legs, leverage, strict=True)
                if p.direction == side
            ),
            start=Fraction(),
        )
        for side in ("long", "short")
    }
    # capital_i = common_notional * within_side_notional_share_i / leverage_i
    coefficients = [Fraction(p.capital_bps, 1) / totals[p.direction] for p in legs]
    common = Fraction(10000, 1) / sum(coefficients, start=Fraction())
    targets = [common * c for c in coefficients]
    floors = [int(t) for t in targets]
    remainder = 10000 - sum(floors)
    best: tuple[Fraction, tuple[str, ...]] | None = None
    allocation = None
    for rounded_up in combinations(range(len(legs)), remainder):
        amounts = [n + (i in rounded_up) for i, n in enumerate(floors)]
        if any(
            n <= 0
            or (
                (p.kind in {"perp", "hip3"} or "/" in p.instrument_id)
                and variant.budget_usd * n * lev / 10000 < 10
            )
            for n, lev, p in zip(amounts, leverage, legs, strict=True)
        ):
            continue
        net = sum(
            (
                n * lev * (1 if p.direction == "long" else -1)
                for n, lev, p in zip(amounts, leverage, legs, strict=True)
            ),
            start=Fraction(),
        )
        if abs(net) > 2:
            continue
        rank = (
            sum(
                (abs(n - target) for n, target in zip(amounts, targets, strict=True)),
                start=Fraction(),
            ),
            tuple(sorted(legs[i].instrument_id for i in rounded_up)),
        )
        if best is None or rank < best:
            best, allocation = rank, amounts
    if allocation is None:
        raise ValueError(
            f"{variant.budget_usd}: matched sizing violates Hyperliquid $10 minimums or rounding; revise the researched holdings, not leverage or cash"
        )
    result = Variant.model_validate(
        {
            **variant.model_dump(),
            "positions": [
                {**p.model_dump(), "capital_bps": n}
                for p, n in zip(legs, allocation, strict=True)
            ],
        }
    )
    if errors := construction_errors(result, construction):
        raise ValueError("\n".join(errors))
    return result
