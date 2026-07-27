from __future__ import annotations

import pytest

from wayfinder_paths.mcp.tools.alpha_lab import research_search_alpha
from wayfinder_paths.mcp.tools.defillama_free import research_defillama_free
from wayfinder_paths.mcp.tools.evm_contract import contracts_call
from wayfinder_paths.mcp.tools.goldsky_direct import research_goldsky_graphql
from wayfinder_paths.mcp.tools.hyperliquid import hyperliquid_place_market_order
from wayfinder_paths.mcp.tools.quotes import onchain_quote_swap
from wayfinder_paths.mcp.tools.runner import core_runner


def _assert_guided_error(
    result: dict,
    *,
    field: str,
    suggestion_field: str | None = None,
) -> None:
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_argument"
    assert result["error"]["details"]["field"] == field
    if suggestion_field is not None:
        assert suggestion_field in result["error"]["details"]["suggested_arguments"]


@pytest.mark.asyncio
async def test_human_amount_guard_runs_before_wallet_or_token_lookup() -> None:
    result = await onchain_quote_swap(
        wallet_label="missing",
        from_token="ethereum",
        to_token="usd-coin",
        amount="100",
    )

    _assert_guided_error(result, field="amount", suggestion_field="amount")
    assert "decimal point" in result["error"]["message"]


@pytest.mark.asyncio
async def test_goldsky_variables_guard_explains_json_object_shape() -> None:
    result = await research_goldsky_graphql(
        endpoint="https://api.goldsky.com/api/public/example/gn",
        query="query { example }",
        variables="[1, 2]",
    )

    _assert_guided_error(result, field="variables", suggestion_field="variables")
    assert "JSON object" in result["error"]["message"]


@pytest.mark.asyncio
async def test_contract_target_guard_runs_before_abi_resolution() -> None:
    result = await contracts_call(
        chain_id=1,
        contract_address="not-an-address",
        function_name="balanceOf",
    )

    _assert_guided_error(
        result,
        field="contract_address",
        suggestion_field="contract_address",
    )


@pytest.mark.asyncio
async def test_runner_schedule_guard_runs_without_a_daemon() -> None:
    result = await core_runner(
        action="add_job",
        name="bad-schedule",
        type="script",
        script_path=".wayfinder_runs/job.py",
        interval_seconds=60,
        cron_expr="0 * * * *",
    )

    _assert_guided_error(result, field="cron_expr", suggestion_field="interval_seconds")
    assert "exactly one" in result["error"]["message"]


@pytest.mark.asyncio
async def test_hyperliquid_sizing_guard_runs_before_market_lookup() -> None:
    result = await hyperliquid_place_market_order(
        wallet_label="main",
        asset_name="BTC-USDC",
        is_buy=True,
    )

    _assert_guided_error(result, field="size", suggestion_field="usd_amount")
    assert "exactly one" in result["error"]["message"]


@pytest.mark.asyncio
async def test_defillama_conditional_argument_has_a_corrected_example() -> None:
    result = await research_defillama_free(dataset="current_prices")

    _assert_guided_error(result, field="coins", suggestion_field="coins")
    assert result["error"]["details"]["suggested_arguments"]["dataset"] == (
        "current_prices"
    )


@pytest.mark.asyncio
async def test_alpha_date_guard_rejects_non_iso_input() -> None:
    result = await research_search_alpha(created_after="last Tuesday")

    _assert_guided_error(
        result,
        field="created_after",
        suggestion_field="created_after",
    )
    assert "ISO-8601" in result["error"]["message"]
