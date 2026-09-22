import numpy as np
import pandas as pd
import pytest

from wayfinder_paths.quant.pattern_match_positioning_outcomes import (
    resolve_positioning_outcome,
)

ENTRY = pd.Timestamp("2026-09-01T00:30Z")


def inputs() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    bars = pd.DataFrame(
        {
            "timestamp": pd.date_range(ENTRY, periods=97, freq="15min"),
            "open": 100.0,
            "close": 120.0,
        }
    )
    benchmark = bars.assign(close=110.0)
    funding = pd.DataFrame(
        {
            "timestamp": pd.date_range(ENTRY.floor("h"), periods=26, freq="h"),
            "funding_rate": 0.001,
        }
    )
    return bars, funding, benchmark, funding.assign(funding_rate=0.002)


@pytest.mark.parametrize("direction", [-1, 1])
@pytest.mark.parametrize("beta", [-2.0, 0.0, 2.0])
def test_two_leg_returns_include_frozen_beta_funding_and_gross_cost(
    direction: int, beta: float
) -> None:
    result = resolve_positioning_outcome(
        *inputs(),
        entry_time=ENTRY,
        as_of=ENTRY + pd.Timedelta(days=1),
        direction=direction,
        hedge_beta=beta,
        cost_bps=9,
    )
    assert result.status == "closed"
    assert result.entry_price == result.hedge_entry_price == 100
    assert result.exit_time == ENTRY + pd.Timedelta(days=1)
    assert result.gross_return == pytest.approx(
        direction * (0.2 - beta * 0.1) / (1 + abs(beta))
    )
    assert result.funding_return == pytest.approx(
        direction * (-0.024 + beta * 0.048) / (1 + abs(beta))
    )
    assert result.gross_return is not None and result.funding_return is not None
    assert result.net_return == pytest.approx(
        result.gross_return + result.funding_return - 0.0009
    )


@pytest.mark.parametrize(
    "hours,status", [(0, "pending"), (1, "open"), (23, "open"), (24, "closed")]
)
def test_exit_requires_24_hours_of_completed_bars(hours: int, status: str) -> None:
    result = resolve_positioning_outcome(
        *inputs(),
        entry_time=ENTRY,
        as_of=ENTRY + pd.Timedelta(hours=hours),
        direction=1,
        hedge_beta=1,
        cost_bps=9,
    )
    assert result.status == status
    if status != "closed":
        assert result.net_return is None and result.exit_time is None


@pytest.mark.parametrize(
    "missing",
    [
        "asset_price",
        "hedge_price",
        "asset_funding",
        "hedge_funding",
        "duplicate_funding",
    ],
)
def test_incomplete_data_never_becomes_a_zero_return(missing: str) -> None:
    bars, rates, benchmark, hedge_rates = inputs()
    if missing == "asset_price":
        bars = bars.drop(index=2)
    elif missing == "hedge_price":
        benchmark = benchmark.drop(index=2)
    elif missing == "asset_funding":
        rates = rates.drop(index=2)
    elif missing == "hedge_funding":
        hedge_rates = hedge_rates.drop(index=2)
    else:
        hedge_rates = pd.concat([hedge_rates, hedge_rates.iloc[2:3]])
    result = resolve_positioning_outcome(
        bars,
        rates,
        benchmark,
        hedge_rates,
        entry_time=ENTRY,
        as_of=ENTRY + pd.Timedelta(days=1),
        direction=1,
        hedge_beta=1,
        cost_bps=9,
    )
    assert result.status == "open" and result.net_return is None
    assert result.data_status == (
        "missing_prices" if "price" in missing else "missing_funding"
    )


def test_future_prices_and_funding_do_not_change_completed_results() -> None:
    data = inputs()
    terms = {
        "entry_time": ENTRY,
        "as_of": ENTRY + pd.Timedelta(days=1),
        "direction": 1,
        "hedge_beta": 1,
        "cost_bps": 9,
    }
    expected = resolve_positioning_outcome(*data, **terms)
    for frame in (data[0], data[2]):
        frame.loc[96, ["open", "close"]] *= 10
    for rates in (data[1], data[3]):
        rates.loc[25, "funding_rate"] = np.nan
    assert resolve_positioning_outcome(*data, **terms) == expected
