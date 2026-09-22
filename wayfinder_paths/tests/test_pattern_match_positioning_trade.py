from math import inf, nan

import pytest

from wayfinder_paths.quant.pattern_match_positioning import positioning_weights
from wayfinder_paths.quant.pattern_match_positioning_trade import (
    positioning_exit_reason,
    size_positioning_trade,
)


@pytest.mark.parametrize("direction", [-1, 1])
@pytest.mark.parametrize("beta", [-3, -1.5, 0, 0.5, 1, 3])
def test_sizing_preserves_signed_gross_weights(direction: int, beta: float) -> None:
    legs = size_positioning_trade(
        direction=direction,
        hedge_beta=beta,
        gross_notional=12_000,
        asset_id=1,
        asset_price=3_000,
        btc_asset_id=0,
        btc_price=60_000,
        size_decimals={1: 4, 0: 5},
    )
    weights = positioning_weights(direction, beta)
    assert sum(abs(weight) for weight in weights) == pytest.approx(1)
    assert len(legs) == (1 if beta == 0 else 2)
    for leg, weight in zip(legs, weights, strict=False):
        assert (leg.signed_size > 0) == (weight > 0)
        assert leg.notional == pytest.approx(abs(weight) * 12_000, abs=0.61)
    assert sum(leg.notional for leg in legs) <= 12_000


def test_rounding_is_downward_and_does_not_increase_the_reviewed_budget() -> None:
    legs = size_positioning_trade(
        direction=1,
        hedge_beta=1.3,
        gross_notional=1_000,
        asset_id=1,
        asset_price=3_123.45,
        btc_asset_id=0,
        btc_price=65_432.1,
        size_decimals={1: 4, 0: 5},
    )
    assert legs[0].signed_size == 0.1391
    assert legs[1].signed_size == -0.00863
    assert sum(leg.notional for leg in legs) <= 1_000


@pytest.mark.parametrize("beta,budget", [(0.01, 100), (3, 30), (1, 19.99)])
def test_a_required_small_leg_is_rejected_not_dropped(
    beta: float, budget: float
) -> None:
    with pytest.raises(ValueError, match="every positioning leg"):
        size_positioning_trade(
            direction=1,
            hedge_beta=beta,
            gross_notional=budget,
            asset_id=1,
            asset_price=3_000,
            btc_asset_id=0,
            btc_price=60_000,
            size_decimals={1: 4, 0: 5},
        )


@pytest.mark.parametrize("beta", [nan, inf, -3.1, 3.1, True])
def test_invalid_beta_is_not_sized(beta: float) -> None:
    with pytest.raises(ValueError, match="hedge beta"):
        positioning_weights(1, beta)


@pytest.mark.parametrize("direction", [0, 2, -2, True])
def test_invalid_direction_is_not_sized(direction: int) -> None:
    with pytest.raises(ValueError, match="direction"):
        positioning_weights(direction, 1)


@pytest.mark.parametrize("asset_id,btc_id", [(1, 1), (-1, 0), (10_000, 0), (True, 0)])
def test_duplicate_and_non_perp_markets_are_rejected(
    asset_id: int, btc_id: int
) -> None:
    with pytest.raises(ValueError, match="amount or duplicate markets"):
        size_positioning_trade(
            direction=1,
            hedge_beta=1,
            gross_notional=1_000,
            asset_id=asset_id,
            asset_price=3_000,
            btc_asset_id=btc_id,
            btc_price=60_000,
            size_decimals={1: 4, 0: 5},
        )


def test_zero_beta_does_not_require_a_btc_order_or_price() -> None:
    legs = size_positioning_trade(
        direction=-1,
        hedge_beta=0,
        gross_notional=30,
        asset_id=1,
        asset_price=3_000,
        btc_asset_id=0,
        btc_price=nan,
        size_decimals={1: 4},
    )
    assert len(legs) == 1
    assert legs[0].signed_size == -0.01


@pytest.mark.parametrize("net", [nan, inf, -inf])
def test_invalid_pnl_is_not_treated_as_a_valid_zero(net: float) -> None:
    with pytest.raises(ValueError, match="finite or unavailable"):
        positioning_exit_reason(
            net_return=net,
            stop_loss_return=0.02,
            take_profit_return=0.03,
            holding_period_elapsed=False,
            leg_reduced=False,
        )


@pytest.mark.parametrize("price", [0, -1, nan, inf])
def test_missing_hedge_price_does_not_become_an_outright_trade(price: float) -> None:
    with pytest.raises(ValueError, match="live price"):
        size_positioning_trade(
            direction=1,
            hedge_beta=1,
            gross_notional=1_000,
            asset_id=1,
            asset_price=3_000,
            btc_asset_id=0,
            btc_price=price,
            size_decimals={1: 4, 0: 5},
        )


@pytest.mark.parametrize("decimals", [{1: 4}, {1: 4, 0: -1}, {1: 4, 0: 100}])
def test_missing_or_invalid_hedge_metadata_is_rejected(
    decimals: dict[int, int],
) -> None:
    with pytest.raises(ValueError, match="size decimals"):
        size_positioning_trade(
            direction=1,
            hedge_beta=1,
            gross_notional=1_000,
            asset_id=1,
            asset_price=3_000,
            btc_asset_id=0,
            btc_price=60_000,
            size_decimals=decimals,
        )


@pytest.mark.parametrize(
    "net,expected",
    [
        (-0.021, "stop_loss"),
        (-0.02, "stop_loss"),
        (-0.019, None),
        (0, None),
        (0.029, None),
        (0.03, "take_profit"),
        (0.031, "take_profit"),
        (None, None),
    ],
)
def test_exit_uses_combined_net_return_not_the_asset_move(
    net: float | None, expected: str | None
) -> None:
    assert (
        positioning_exit_reason(
            net_return=net,
            stop_loss_return=0.02,
            take_profit_return=0.03,
            holding_period_elapsed=False,
            leg_reduced=False,
        )
        == expected
    )


@pytest.mark.parametrize(
    "expired,reduced,expected",
    [
        (True, False, "time_limit"),
        (False, True, "leg_reduced"),
        (True, True, "leg_reduced"),
    ],
)
def test_time_and_orphaned_hedge_exits_do_not_wait_for_pnl(
    expired: bool, reduced: bool, expected: str
) -> None:
    assert (
        positioning_exit_reason(
            net_return=None,
            stop_loss_return=0.02,
            take_profit_return=0.03,
            holding_period_elapsed=expired,
            leg_reduced=reduced,
        )
        == expected
    )


@pytest.mark.parametrize(
    "stop,take", [(0, 0.02), (0.02, 0), (-0.1, 0.02), (nan, 0.02), (0.02, inf)]
)
def test_invalid_exit_bounds_are_rejected(stop: float, take: float) -> None:
    with pytest.raises(ValueError, match="stop-loss and take-profit"):
        positioning_exit_reason(
            net_return=0,
            stop_loss_return=stop,
            take_profit_return=take,
            holding_period_elapsed=False,
            leg_reduced=False,
        )
