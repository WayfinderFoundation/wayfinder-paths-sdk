from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.mcp.resources import delta_lab


class TestSearchDeltaLabAssets:
    @pytest.mark.asyncio
    async def test_calls_client_search(self):
        mock = AsyncMock(return_value={"assets": [], "total_count": 0})
        with patch.object(delta_lab.DELTA_LAB_CLIENT, "search_assets", mock):
            result = await delta_lab.research_search_delta_lab_assets("sUSDai")
        mock.assert_awaited_once_with(query="sUSDai", chain_id=None, limit=25)
        assert result == {"assets": [], "total_count": 0}

    @pytest.mark.asyncio
    async def test_chain_code_is_mapped_to_chain_id(self):
        mock = AsyncMock(return_value={"assets": [], "total_count": 0})
        with patch.object(delta_lab.DELTA_LAB_CLIENT, "search_assets", mock):
            result = await delta_lab.research_search_delta_lab_assets(
                "usdc", chain="base"
            )
        mock.assert_awaited_once_with(query="usdc", chain_id=8453, limit=25)
        assert result["total_count"] == 0

    @pytest.mark.asyncio
    async def test_limit_is_parsed(self):
        mock = AsyncMock(return_value={"assets": [], "total_count": 0})
        with patch.object(delta_lab.DELTA_LAB_CLIENT, "search_assets", mock):
            result = await delta_lab.research_search_delta_lab_assets(
                "usdc", chain="all", limit="10"
            )
        mock.assert_awaited_once_with(query="usdc", chain_id=None, limit=10)
        assert result["total_count"] == 0

    @pytest.mark.asyncio
    async def test_unknown_chain_returns_error(self):
        result = await delta_lab.research_search_delta_lab_assets(
            "usdc", chain="unknown"
        )
        assert result["error"] == "unknown chain filter: 'unknown'"


class TestScreenBorrowRoutes:
    @pytest.mark.asyncio
    async def test_chain_code_is_mapped_to_chain_id(self):
        mock = AsyncMock(return_value={"data": [], "count": 0})
        with patch.object(delta_lab.DELTA_LAB_CLIENT, "screen_borrow_routes", mock):
            result = await delta_lab.research_screen_borrow_routes(chain_id="base")
        mock.assert_awaited_once()
        assert mock.call_args.kwargs["chain_id"] == 8453
        assert result == {"data": [], "count": 0}

    @pytest.mark.asyncio
    async def test_unknown_chain_returns_error(self):
        result = await delta_lab.research_screen_borrow_routes(chain_id="unknown")
        assert result["error"] == "unknown chain filter: 'unknown'"
