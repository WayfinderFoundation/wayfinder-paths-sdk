from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp.utilities.func_metadata import func_metadata

from wayfinder_paths.mcp.tools import research_gateway
from wayfinder_paths.mcp.tools.alpha_lab import research_search_alpha
from wayfinder_paths.mcp.tools.defillama_free import research_defillama_free
from wayfinder_paths.mcp.tools.delta_lab import research_search_delta_lab_assets


@pytest.mark.asyncio
async def test_defillama_returns_allowed_values_for_bad_dataset() -> None:
    result = await research_defillama_free(dataset="stablecoin")

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_argument"
    assert result["error"]["details"]["field"] == "dataset"
    assert "stablecoins" in result["error"]["details"]["allowed_values"]


@pytest.mark.asyncio
async def test_defillama_accepts_int_limit_for_missing_required_dataset_arg() -> None:
    result = await research_defillama_free(dataset="current_prices", limit=5)

    assert result["ok"] is False
    assert result["error"]["code"] == "error"
    assert "coins is required" in result["error"]["message"]


@pytest.mark.asyncio
async def test_alpha_returns_allowed_values_for_bad_scan_type() -> None:
    result = await research_search_alpha(scan_type="twitter")

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_argument"
    assert result["error"]["details"]["field"] == "scan_type"
    assert "twitter_post" in result["error"]["details"]["allowed_values"]


@pytest.mark.asyncio
async def test_delta_lab_limit_type_errors_are_structured() -> None:
    result = await research_search_delta_lab_assets(query="ETH", limit="many")

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_argument"
    assert result["error"]["details"]["field"] == "limit"


@pytest.mark.asyncio
async def test_mcp_metadata_ignores_extra_research_tool_args(monkeypatch) -> None:
    fake_client = type(
        "FakeResearchClient",
        (),
        {"search": AsyncMock(return_value={"results": []})},
    )()
    monkeypatch.setattr(research_gateway, "RESEARCH_CLIENT", fake_client)

    metadata = func_metadata(research_gateway.core_web_search)
    schema = metadata.arg_model.model_json_schema(by_alias=True)

    assert schema.get("additionalProperties") is not False
    result = await metadata.call_fn_with_arg_validation(
        research_gateway.core_web_search,
        fn_is_async=True,
        arguments_to_validate={
            "query": "ethena catalyst",
            "numResults": 5,
            "type": "news",
            "unused": "ignored",
            "anotherUnused": {"nested": True},
        },
        arguments_to_pass_directly=None,
    )

    assert result["ok"] is True
    kwargs = fake_client.search.await_args.kwargs
    assert kwargs["num_results"] == 5
    assert kwargs["search_type"] == "auto"
    assert kwargs["category"] == "news"
    assert "unused" not in kwargs
