from __future__ import annotations

import pandas as pd
import pytest
from pydantic import ValidationError

from wayfinder_paths.core.theses.backtest import MarketHistory, backtest_variant
from wayfinder_paths.core.theses.models import Position, Proposal, Variant


def position(**changes) -> Position:
    return Position.model_validate(
        {
            "id": "btc",
            "component_id": "growth",
            "kind": "perp",
            "instrument_id": "BTC-USDC",
            "symbol": "BTC",
            "direction": "long",
            "capital_bps": 9000,
            "leverage": 1,
            "rationale": "Thesis expression",
            **changes,
        }
    )


def market(prices=(100, 100, 110, 120), **changes) -> MarketHistory:
    index = pd.date_range("2026-09-01", periods=len(prices), freq="h", tz="UTC")
    frame = pd.DataFrame(
        dict.fromkeys(("open", "high", "low", "close"), prices), index=index
    )
    return MarketHistory(
        **{
            "prices": frame,
            "source": "fixture",
            "fee_rate": 0,
            "slippage_rate": 0,
            "routing_cost_usd": 0,
            "min_notional_usd": 10,
            "max_notional_usd": 200000,
            "funding": pd.Series(0.0, index=index),
            "maintenance_margin_rate": 0.05,
            **changes,
        }
    )


def run(p: Position | None = None, m: MarketHistory | None = None, budget=100) -> dict:
    p = p or position()
    return backtest_variant(
        Variant(
            budget_usd=budget,
            rationale="Sized independently",
            positions=[p],
            cash_bps=10000 - p.capital_bps,
        ),
        {p.instrument_id: m or market()},
        end=pd.Timestamp("2026-09-01T04:00:00Z"),
    )


def test_hold_does_not_rebalance_or_enter_on_signal_bar():
    result = run(m=market((10, 100, 110, 120)))
    assert result["equity"][0]["value"] == 100
    assert result["positions"][0]["entry_price"] == 100
    assert result["equity"][-1]["value"] == 118
    assert result["total_return"] == pytest.approx(0.18)
    assert result["coverage"]["partial"]


def test_costs_fit_inside_budget_and_do_not_scale_as_free_money():
    result = run(m=market((100, 100, 100, 100), fee_rate=0.01, routing_cost_usd=2))
    assert result["equity"][-1]["value"] == pytest.approx(10 + 88 / 1.01)
    assert result["fees_usd"] == pytest.approx(88 / 1.01 * 0.01)
    assert result["routing_cost_usd"] == 2


def test_short_receives_positive_funding():
    m = market((100, 100, 100, 100))
    m.funding[:] = 0.01
    result = run(position(direction="short"), m)
    assert result["funding_paid_usd"] == pytest.approx(-1.8)
    assert result["equity"][-1]["value"] == pytest.approx(101.8)


def test_isolated_liquidation_preserves_uncommitted_cash():
    result = run(position(leverage=2), market((100, 100, 1, 200)))
    assert result["liquidated"]
    assert result["equity"][-1]["value"] == 10


def test_stop_stays_closed_even_after_rebound():
    result = run(position(stop_loss_pct=0.1), market((100, 100, 85, 200)))
    assert result["positions"][0]["close_reason"] == "protective_exit"
    assert result["equity"][-1]["value"] == 86.5


def test_intrabar_liquidation_precedes_ambiguous_protective_exit():
    m = market((100, 100, 100, 200))
    m.prices.loc[m.prices.index[2], "low"] = 1
    result = run(position(leverage=2, stop_loss_pct=0.1, take_profit_pct=0.1), m)
    assert result["liquidated"]
    assert result["equity"][-1]["value"] == 10


def test_short_intrabar_liquidation_uses_high():
    m = market((100, 100, 100, 80))
    m.prices.loc[m.prices.index[2], "high"] = 200
    assert run(position(leverage=2, direction="short"), m)["liquidated"]


@pytest.mark.parametrize(
    "kind,direction,instrument",
    [
        ("token", "long", "ethereum-base"),
        ("hip3", "short", "xyz:SP500"),
        ("prediction", "yes", "123"),
        ("prediction", "no", "456"),
    ],
)
def test_instrument_accounting(kind, direction, instrument):
    p = position(kind=kind, direction=direction, instrument_id=instrument)
    m = market((0.5, 0.5, 0.6, 0.7)) if kind == "prediction" else market()
    result = run(p, m)
    assert result["total_return"] == pytest.approx(
        0.36 if kind == "prediction" else (-0.18 if direction == "short" else 0.18)
    )


def test_prediction_settles_to_cash_without_fabricated_post_resolution_prices():
    p = position(kind="prediction", direction="no", instrument_id="123")
    m = market(
        (0.5, 0.5),
        settlement_at=pd.Timestamp("2026-09-01T02:00:00Z"),
        settlement_price=1,
    )
    result = run(p, m)
    assert result["positions"][0]["close_reason"] == "settlement"
    assert result["equity"][-1]["value"] == 190


@pytest.mark.parametrize("missing", ["price", "funding"])
def test_internal_gaps_fail_closed(missing):
    m = market()
    if missing == "price":
        m.prices.loc[m.prices.index[2], "close"] = float("nan")
    else:
        m.funding.loc[m.funding.index[2]] = float("nan")
    with pytest.raises(ValueError, match="[Mm]issing"):
        run(m=m)


def test_distinct_budgets_enforce_minimums():
    m = market(min_notional_usd=100)
    with pytest.raises(ValueError, match="executable size"):
        run(m=m)
    assert run(m=m, budget=1000)["budget_usd"] == 1000


@pytest.mark.parametrize(
    "changes",
    [
        {"leverage": 3},
        {"kind": "token", "direction": "short"},
        {"kind": "prediction", "direction": "long"},
        {"kind": "token", "stop_loss_pct": 0.1},
        {"kind": "hip3"},
        {"leverage": float("nan")},
        {"capital_bps": True},
    ],
)
def test_invalid_position_contract(changes):
    with pytest.raises(ValidationError):
        position(**changes)


def test_all_four_budget_variants_required():
    with pytest.raises(ValidationError):
        Proposal.model_validate(
            {
                "title": "Growth",
                "interpretation": "Growth",
                "intent": "absolute",
                "assumptions": ["Initial setup only"],
                "components": [],
                "variants": [],
            }
        )


@pytest.mark.parametrize("budget", [100, 1000, 10000, 100000])
def test_each_budget_is_simulated_as_funded_capital(budget):
    result = run(budget=budget)
    assert result["equity"][0]["value"] == budget
    assert result["equity"][-1]["value"] == pytest.approx(budget * 1.18)


def test_liquidation_does_not_spend_another_positions_collateral():
    perp = position(capital_bps=5000, leverage=2)
    spot = position(
        id="eth", instrument_id="ethereum-base", kind="token", capital_bps=4000
    )
    variant = Variant(
        budget_usd=100,
        rationale="Two funded sleeves",
        positions=[perp, spot],
        cash_bps=1000,
    )
    result = backtest_variant(
        variant,
        {perp.instrument_id: market((100, 100, 1, 200)), spot.instrument_id: market()},
        end=pd.Timestamp("2026-09-01T04:00:00Z"),
    )
    assert result["equity"][-1]["value"] == pytest.approx(58)


def test_short_window_annualization_is_unavailable_not_infinite():
    from wayfinder_paths.core.backtesting.execution.simulator import _cagr

    assert _cagr(100, 200, 1, 8760) is None
    assert _cagr(100, 120, 8760, 8760) == pytest.approx(0.2)
