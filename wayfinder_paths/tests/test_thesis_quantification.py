import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from wayfinder_paths.core.clients.TokenClient import TokenClient
from wayfinder_paths.core.theses.models import Construction, Position, Variant
from wayfinder_paths.core.theses.quantification import (
    DAY_MS,
    allocation_key,
    daily_returns,
    price_metrics,
    quantify_variants,
)
from wayfinder_paths.mcp.polymarket_summary import compact_order_book
from wayfinder_paths.mcp.tools import thesis_quantification as tool


@pytest.fixture(autouse=True)
def clear_public_cache():
    tool._market_cache.clear()
    yield
    tool._market_cache.clear()


def variant(**changes):
    position = Position.model_validate(
        {
            "id": "holding",
            "component_id": "thesis",
            "kind": "perp",
            "instrument_id": "BTC-USDC",
            "symbol": "BTC",
            "direction": "long",
            "capital_bps": 5000,
            "leverage": 1,
            "rationale": "Fixture",
            **changes,
        }
    )
    return Variant(
        budget_usd=100,
        rationale="Fixture",
        positions=[position],
        cash_bps=10000 - position.capital_bps,
    )


def test_missing_days_are_not_filled_or_counted_as_one_day_returns():
    prices = {0: 100, DAY_MS: 110, 3 * DAY_MS: 150}
    assert daily_returns(prices) == {DAY_MS: pytest.approx(0.1)}
    metrics = price_metrics(prices)
    assert metrics["price_return"] == 0.5
    assert metrics["daily_returns"] == 1
    assert metrics["annualized_daily_volatility"] is None
    assert metrics["status"] == "limited_history"


@pytest.mark.parametrize(
    "direction,leverage,expected",
    [("long", 1, 0.1), ("short", 1, -0.1), ("short", 2, -0.2)],
)
def test_fixed_notional_price_pnl_cash_and_funding_sign(direction, leverage, expected):
    draft = variant(direction=direction, leverage=leverage)
    report = quantify_variants(
        [draft],
        {"BTC-USDC": {"prices": {0: 100, DAY_MS: 120}, "funding": {"sum_rates": 0.01}}},
    )
    portfolio = report["portfolios"][0]
    assert portfolio["metrics"]["price_return"] == pytest.approx(expected)
    assert portfolio["cash_bps"] == 5000
    assert portfolio["gross_notional_bps"] == 5000 * leverage
    assert portfolio["funding"][0]["observed_cost_nav_fraction"] == pytest.approx(
        0.005 * leverage * (-1 if direction == "short" else 1)
    )
    # Same weights scale across budgets; prose and stop orders are not simulated.
    assert allocation_key(draft) == allocation_key(
        draft.model_copy(update={"budget_usd": 100000, "rationale": "Larger"})
    )
    assert allocation_key(draft) != allocation_key(variant(capital_bps=4000))


def test_no_shares_are_not_a_short_of_yes():
    report = quantify_variants(
        [variant(kind="prediction", instrument_id="123", direction="no")],
        {"123": {"prices": {0: 0.4, DAY_MS: 0.6}}},
    )
    portfolio = report["portfolios"][0]
    assert portfolio["metrics"]["price_return"] == pytest.approx(0.25)
    assert portfolio["signed_directional_notional_bps"] == 0
    assert portfolio["prediction_capital_bps"] == 5000


@pytest.mark.parametrize("short_rate", [0.0015694498, -0.0015694498, 0])
def test_funding_total_weights_signed_notionals_once_across_budgets(short_rate):
    gold = variant(instrument_id="xyz:GOLD", kind="hip3").positions[0]
    btc = variant(id="hedge", direction="short", leverage=2).positions[0]
    drafts = [
        Variant(budget_usd=b, rationale="Pair", positions=[gold, btc], cash_bps=0)
        for b in (100, 1000, 10000, 100000)
    ]
    markets = {
        key: {
            "funding": {
                "sum_rates": rate,
                "observed_hours": 168,
                "expected_hours": 168,
                "start_ms": 0,
                "end_ms": 7 * DAY_MS,
            }
        }
        for key, rate in [("xyz:GOLD", 0.0012425475), ("BTC-USDC", short_rate)]
    }
    report = quantify_variants(drafts, markets)
    for portfolio in report["portfolios"]:
        total = portfolio["funding_summary"]
        assert total["status"] == "measured"
        assert total["start_ms"] == 0
        assert total["end_ms"] == 7 * DAY_MS
        assert total["observed_cost_nav_fraction"] == pytest.approx(
            0.5 * 0.0012425475 - short_rate
        )
        assert total["observed_cost_nav_fraction"] == pytest.approx(
            sum(row["observed_cost_nav_fraction"] for row in portfolio["funding"])
        )
        assert total["observed_cost_nav_pct"] == pytest.approx(
            total["observed_cost_nav_fraction"] * 100
        )
        assert total["observed_net_flow"] == (
            "paid" if total["observed_cost_nav_fraction"] > 0 else "received"
        )
    assert "already NAV-weighted" in report["method"]
    assert "positive is paid, negative is received" in report["method"]


@pytest.mark.parametrize(
    "rate,expected_pct,expected_flow",
    [
        (0.0001335429, 0.01335429, "paid"),
        (0.00098528844, 0.098528844, "paid"),
        (-0.0001335429, -0.01335429, "received"),
        (0, 0, "zero"),
    ],
)
def test_funding_display_preserves_measured_sign_and_percentage_scale(
    rate: float, expected_pct: float, expected_flow: str
) -> None:
    report = quantify_variants(
        [variant(capital_bps=10000)],
        {
            "BTC-USDC": {
                "funding": {
                    "sum_rates": rate,
                    "observed_hours": 168,
                    "expected_hours": 168,
                    "start_ms": 0,
                    "end_ms": 7 * DAY_MS,
                }
            }
        },
    )
    portfolio = report["portfolios"][0]
    total = portfolio["funding_summary"]
    assert total["status"] == "measured"
    assert total["observed_cost_nav_fraction"] == pytest.approx(rate)
    assert total["observed_cost_nav_pct"] == pytest.approx(expected_pct)
    assert total["observed_net_flow"] == expected_flow
    assert portfolio["funding"][0]["observed_cost_nav_pct"] == pytest.approx(
        expected_pct
    )


@pytest.mark.parametrize(
    "bad_funding",
    [
        {},
        {"sum_rates": None},
        {"sum_rates": 0.01, "observed_hours": 167},
        {"sum_rates": 0.01, "start_ms": DAY_MS, "end_ms": 8 * DAY_MS},
        {"sum_rates": 0.01, "start_ms": None},
    ],
)
def test_funding_total_never_sums_missing_partial_or_mismatched_windows(bad_funding):
    draft = Variant(
        budget_usd=100,
        rationale="Pair",
        positions=[
            variant().positions[0],
            variant(id="other", instrument_id="ETH-USDC").positions[0],
        ],
        cash_bps=0,
    )
    good = {
        "sum_rates": 0.01,
        "observed_hours": 168,
        "expected_hours": 168,
        "start_ms": 0,
        "end_ms": 7 * DAY_MS,
    }
    bad = {**good, **bad_funding} if bad_funding else {}
    report = quantify_variants(
        [draft], {"BTC-USDC": {"funding": good}, "ETH-USDC": {"funding": bad}}
    )
    total = report["portfolios"][0]["funding_summary"]
    assert total["status"] == "unavailable"
    assert total["observed_cost_nav_fraction"] is None
    assert total["observed_cost_nav_pct"] is None
    assert total["observed_net_flow"] is None
    assert total["start_ms"] is None and total["end_ms"] is None
    assert report["portfolios"][0]["funding"][0]["observed_cost_nav_fraction"] == 0.005


def test_no_perps_have_zero_funding_not_zero_total_cost():
    report = quantify_variants(
        [variant(kind="token", instrument_id="UBTC/USDC")], {"UBTC/USDC": {}}
    )
    summary = report["portfolios"][0]["funding_summary"]
    assert summary["status"] == "not_applicable"
    assert summary["observed_cost_nav_fraction"] == 0
    assert summary["observed_cost_nav_pct"] == 0
    assert summary["observed_net_flow"] is None
    assert summary["start_ms"] is None and summary["end_ms"] is None
    assert "not total holding costs or a forecast" in report["method"]


def test_zero_outcome_is_a_real_loss_not_a_missing_price():
    draft = variant(kind="prediction", instrument_id="123", direction="no")
    result = quantify_variants([draft], {"123": {"prices": {0: 0.4, DAY_MS: 0}}})
    assert result["assets"]["123"]["metrics"]["price_return"] == -1
    assert result["portfolios"][0]["metrics"]["price_return"] == -0.5


@pytest.mark.asyncio
async def test_funding_failure_does_not_erase_good_price_history():
    rows = [{"t": 0, "T": DAY_MS - 1, "c": "100"}]
    with (
        patch.object(
            tool.HYPERLIQUID_DATA_CLIENT,
            "get_candles_response",
            AsyncMock(return_value={"rows": rows}),
        ),
        patch.object(
            tool.HYPERLIQUID_DATA_CLIENT,
            "get_funding_history",
            AsyncMock(side_effect=TimeoutError),
        ),
    ):
        result = await tool._read_market(variant().positions[0], 0, DAY_MS)
    assert result["prices"] == {0: 100}
    assert result["funding"]["sum_rates"] is None


def test_missing_leg_never_becomes_cash_or_zero_risk():
    report = quantify_variants(
        [variant()], {"BTC-USDC": {"prices": {}, "error": "Provider unavailable"}}
    )
    portfolio = report["portfolios"][0]
    assert portfolio["metrics"]["status"] == "unavailable"
    assert "price_return" not in portfolio["metrics"]
    assert portfolio["missing_history"] == ["BTC-USDC"]
    assert portfolio["funding"][0]["observed_cost_nav_fraction"] is None
    assert portfolio["funding"][0]["observed_cost_nav_pct"] is None
    json.dumps(report, allow_nan=False)


def test_insolvent_gross_diagnostic_cannot_report_plausible_performance():
    result = quantify_variants(
        [variant(direction="short", leverage=2)],
        {"BTC-USDC": {"prices": {0: 100, DAY_MS: 250}}},
    )
    assert result["portfolios"][0]["metrics"]["status"] == "unavailable"


@pytest.mark.parametrize("mode", ["aligned", "disjoint", "constant"])
def test_correlations_align_timestamps_and_reject_insufficient_variance(mode):
    left = {i * DAY_MS: 100 + i * i for i in range(30)}
    right = {
        i * DAY_MS: (100 if mode == "constant" else 2 * (100 + i * i))
        for i in range(
            40 if mode == "disjoint" else 0, 70 if mode == "disjoint" else 30
        )
    }
    report = quantify_variants([], {"A": {"prices": left}, "B": {"prices": right}})
    assert (
        report["correlation_basis"]
        == "instrument_price_returns_before_position_direction"
    )
    row = report["correlations"][0]
    assert row["correlation"] == (pytest.approx(1) if mode == "aligned" else None)


@pytest.mark.parametrize(
    "left_direction,right_direction,expected",
    [
        ("long", "long", 1),
        ("short", "long", -1),
        ("long", "short", -1),
        ("short", "short", 1),
    ],
)
def test_position_correlations_apply_directions_without_changing_raw_matrix(
    left_direction, right_direction, expected
):
    draft = Variant(
        budget_usd=1000,
        rationale="Different direction from price correlation",
        positions=[
            variant(
                id="left", instrument_id="AAA-USDC", direction=left_direction
            ).positions[0],
            variant(
                id="right",
                instrument_id="BBB-USDC",
                direction=right_direction,
                leverage=2,
            ).positions[0],
        ],
        cash_bps=0,
    )
    prices = {i * DAY_MS: 100 + i * i for i in range(30)}
    report = quantify_variants(
        [
            draft.model_copy(update={"budget_usd": b})
            for b in (100, 1000, 10000, 100000)
        ],
        {
            "AAA-USDC": {"prices": prices},
            "BBB-USDC": {"prices": prices},
            "unused-alternative": {"prices": prices},
        },
    )
    assert len(report["correlations"]) == 3
    assert all(r["correlation"] == pytest.approx(1) for r in report["correlations"])
    assert report["position_correlations"] == [
        {
            "left": "AAA-USDC",
            "right": "BBB-USDC",
            "left_direction": left_direction,
            "right_direction": right_direction,
            "overlapping_daily_returns": 29,
            "correlation": pytest.approx(expected),
        }
    ]


@pytest.mark.parametrize("history", ["complete", "short", "constant", "disjoint"])
def test_position_correlations_preserve_unknown_and_do_not_invert_no_shares(history):
    draft = Variant(
        budget_usd=100,
        rationale="NO is long its own outcome price, not short YES",
        positions=[
            variant(
                id="outcome", kind="prediction", instrument_id="123", direction="no"
            ).positions[0],
            variant(id="perp", direction="short").positions[0],
        ],
        cash_bps=0,
    )
    prices = {
        i * DAY_MS: 0.2 if history == "constant" else 0.2 + i * i / 2000
        for i in range(5 if history == "short" else 30)
    }
    report = quantify_variants(
        [draft],
        {
            "123": {"prices": prices},
            "BTC-USDC": {
                "prices": {
                    t + (40 * DAY_MS if history == "disjoint" else 0): p * 100
                    for t, p in prices.items()
                }
            },
        },
    )
    row = report["position_correlations"][0]
    assert row["left_direction"] == "no"
    assert row["right_direction"] == "short"
    assert row["correlation"] == (pytest.approx(-1) if history == "complete" else None)
    assert "direction-adjusted instrument daily returns" in report["method"]
    assert "not portfolio beta or net PnL correlation" in report["method"]


def test_shared_position_matrix_keeps_different_directions_across_budgets():
    drafts = [
        Variant(
            budget_usd=budget,
            rationale="Same instruments, different direction",
            positions=[
                variant(direction=direction).positions[0],
                variant(id="other", instrument_id="ETH-USDC").positions[0],
            ],
            cash_bps=0,
        )
        for budget, direction in [(100, "long"), (1000, "short")]
    ]
    prices = {i * DAY_MS: 100 + i * i for i in range(30)}
    report = quantify_variants(
        drafts, {"BTC-USDC": {"prices": prices}, "ETH-USDC": {"prices": prices}}
    )
    rows = report["position_correlations"]
    assert len(rows) == 2
    assert {(r["left_direction"], r["right_direction"]) for r in rows} == {
        ("long", "long"),
        ("short", "long"),
    }
    assert [r["correlation"] for r in rows] == pytest.approx([1, -1])


@pytest.mark.parametrize("ask", [0.2, 0.28, 0.99, 0, 1, None])
def test_prediction_payoff_is_entry_hurdle_not_probability_forecast(ask):
    book = compact_order_book(
        {"asks": [] if ask is None else [{"price": str(ask), "size": "100"}]}
    )
    payoff = book["buyPayoff"]
    if ask is None or ask in {0, 1}:
        assert payoff is None
    else:
        assert payoff["breakEvenProbabilityBeforeCosts"] == ask
        assert payoff["grossPayoutMultipleBeforeCosts"] == pytest.approx(1 / ask)
        assert payoff["winReturnBeforeCosts"] == pytest.approx(1 / ask - 1)
        assert payoff["grossPayoutMultipleBeforeCosts"] == pytest.approx(
            1 + payoff["winReturnBeforeCosts"]
        )
        assert payoff["lossReturn"] == -1


@pytest.mark.asyncio
async def test_batch_shares_market_reads_across_all_budgets_and_returns_observed_key():
    drafts = [
        variant().model_copy(update={"budget_usd": b})
        for b in (100, 1000, 10000, 100000)
    ]
    with (
        patch.object(
            tool,
            "_read_market",
            AsyncMock(return_value={"prices": {0: 100, DAY_MS: 110}}),
        ) as read,
        patch("wayfinder_paths.mcp.utils._report_tool_metric"),
        patch.object(tool.time, "time", return_value=90 * DAY_MS / 1000),
    ):
        result = await tool.research_quantify_portfolio(drafts)
    assert result["ok"]
    assert read.await_count == 1
    report = result["result"]["portfolio_quantification"]
    assert len(report["portfolios"]) == 4
    assert {p["allocation_key"] for p in report["portfolios"]} == {
        allocation_key(drafts[0])
    }
    assert report["assets"]["BTC-USDC"]["coverage_fraction"] == pytest.approx(2 / 90)


@pytest.mark.asyncio
async def test_construction_sizes_before_measuring_and_returns_exact_draft():
    long = variant(instrument_id="AI-USDC", capital_bps=7000).positions[0]
    short = variant(direction="short", capital_bps=3000).positions[0]
    short = short.model_copy(update={"id": "benchmark"})
    draft = Variant(
        budget_usd=100, cash_bps=0, rationale="Fixture", positions=[long, short]
    )
    construction = Construction(
        mode="matched_relative",
        benchmark="Bitcoin",
        benchmark_direction="short",
        benchmark_instrument_id="BTC-USDC",
    )
    with (
        patch.object(
            tool,
            "_read_market",
            AsyncMock(return_value={"prices": {0: 100, DAY_MS: 110}}),
        ) as read,
        patch("wayfinder_paths.mcp.utils._report_tool_metric"),
    ):
        result = await tool.research_quantify_portfolio(
            [draft], construction=construction
        )
    assert result["ok"]
    assert read.await_count == 2
    sized = Variant.model_validate(result["result"]["sized_variants"][0])
    assert [p.capital_bps for p in sized.positions] == [5000, 5000]
    report = result["result"]["portfolio_quantification"]["portfolios"][0]
    assert report["allocation_key"] == allocation_key(sized)
    assert report["metrics"]["price_return"] == pytest.approx(0)


@pytest.mark.asyncio
async def test_provider_failure_is_reported_without_aborting_other_finalists():
    with (
        patch.object(tool, "_read_market", AsyncMock(side_effect=TimeoutError)),
        patch("wayfinder_paths.mcp.utils._report_tool_metric"),
    ):
        result = await tool.research_quantify_portfolio([variant()])
    assert result["ok"]
    assert (
        result["result"]["portfolio_quantification"]["assets"]["BTC-USDC"]["error"]
        == "Market read timed out"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("end_offset", [0, -1])
async def test_spot_history_resolves_lookup_id_and_uses_completed_daily_window(
    end_offset,
):
    token = {
        "token_id": "base_0xabc",
        "address": "0xabc",
        "chain": {"id": 8453, "code": "base"},
        "identity": {"is_canonical": False},
    }
    rows = [
        {"t": i * DAY_MS, "T": (i + 1) * DAY_MS + end_offset, "c": "2"}
        for i in range(3)
    ]
    with (
        patch.object(
            tool.TOKEN_CLIENT, "get_token_details", AsyncMock(return_value=token)
        ) as resolve,
        patch.object(
            tool.TOKEN_CLIENT, "get_candles", AsyncMock(return_value=rows)
        ) as candles,
    ):
        result = await tool._read_market(
            variant(kind="token", instrument_id="aerodrome-finance-base").positions[0],
            0,
            2 * DAY_MS,
        )
    resolve.assert_awaited_once_with("aerodrome-finance-base")
    candles.assert_awaited_once_with(
        "base_0xabc", "1d", chain_id=8453, start_ms=0, end_ms=2 * DAY_MS
    )
    assert result["prices"] == {0: 2, DAY_MS: 2}
    assert result["resolved_token"] == {
        **token,
        "symbol": None,
        "name": None,
        "lookup_id": "aerodrome-finance-base",
    }


@pytest.mark.asyncio
async def test_quantification_retains_spot_identity_when_history_is_unavailable():
    token = {
        "token_id": "base_0xabc",
        "address": "0xabc",
        "chain": {"id": 8453, "code": "base"},
        "identity": {"suspicious": False},
    }
    with (
        patch.object(
            tool.TOKEN_CLIENT, "get_token_details", AsyncMock(return_value=token)
        ),
        patch.object(
            tool.TOKEN_CLIENT, "get_candles", AsyncMock(side_effect=TimeoutError)
        ),
        patch("wayfinder_paths.mcp.utils._report_tool_metric"),
    ):
        result = await tool.research_quantify_portfolio(
            [variant(kind="token", instrument_id="project-base")]
        )
    asset = result["result"]["portfolio_quantification"]["assets"]["project-base"]
    assert asset["resolved_token"]["token_id"] == "base_0xabc"
    assert asset["resolved_token"]["lookup_id"] == "project-base"
    assert asset["history_error"] == "Spot history timed out"
    assert asset["coverage_fraction"] == 0


@pytest.mark.asyncio
async def test_quantification_does_not_publish_conflicting_spot_identity():
    with (
        patch.object(
            tool.TOKEN_CLIENT,
            "get_token_details",
            AsyncMock(return_value={"identity": {"suspicious": True}}),
        ),
        patch.object(tool.TOKEN_CLIENT, "get_candles", AsyncMock()) as candles,
        patch("wayfinder_paths.mcp.utils._report_tool_metric"),
    ):
        result = await tool.research_quantify_portfolio(
            [variant(kind="token", instrument_id="project-base")]
        )
    asset = result["result"]["portfolio_quantification"]["assets"]["project-base"]
    assert "conflicting identity" in asset["error"]
    assert "resolved_token" not in asset
    candles.assert_not_awaited()


@pytest.mark.asyncio
async def test_token_client_keeps_legacy_candles_and_adds_range_parameters():
    client = TokenClient()
    response = httpx.Response(
        200,
        json={"rows": [{"c": "1"}]},
        request=httpx.Request("GET", "https://example.test"),
    )
    with patch.object(
        client, "_authed_request", AsyncMock(return_value=response)
    ) as request:
        assert await client.get_candles("asset-base", "1d", chain_id=8453) == [
            {"c": "1"}
        ]
        assert "start_ms" not in request.call_args.kwargs["params"]
        await client.get_candles(
            "asset-base", "1d", chain_id=8453, start_ms=0, end_ms=DAY_MS
        )
        assert request.call_args.kwargs["params"]["start_ms"] == 0
        with pytest.raises(ValueError):
            await client.get_candles("asset-base", "1d", chain_id=8453, start_ms=0)


@pytest.mark.asyncio
async def test_prediction_resolution_and_book_survive_missing_history():
    adapter = AsyncMock()
    adapter.get_market_by_token_id.return_value = (
        True,
        {
            "id": "1",
            "outcomes": ["Yes", "No"],
            "clobTokenIds": ["123", "456"],
            "active": True,
        },
    )
    adapter.resolve_outcome_from_token_id = lambda **_: "No"
    adapter.get_prices_history.return_value = (False, "No history")
    adapter.get_order_book.return_value = (
        True,
        {"asks": [{"price": ".4", "size": "100"}]},
    )
    with patch.object(tool, "PolymarketAdapter", return_value=adapter):
        result = await tool._read_market(
            variant(kind="prediction", direction="no", instrument_id="456").positions[
                0
            ],
            0,
            90 * DAY_MS,
        )
    assert result["prices"] == {}
    assert result["book"]["buyPayoff"]["grossPayoutMultipleBeforeCosts"] == 2.5
    assert result["book"]["buyPayoff"]["winReturnBeforeCosts"] == 1.5
    adapter.close.assert_awaited_once()
