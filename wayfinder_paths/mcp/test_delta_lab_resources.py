from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.mcp.resources import delta_lab


class TestGetAssetsByAddress:
    @pytest.mark.asyncio
    async def test_all_chain_filter_passes_none(self):
        mock = AsyncMock(return_value={"assets": []})
        with patch.object(delta_lab.DELTA_LAB_CLIENT, "get_assets_by_address", mock):
            result = await delta_lab.get_assets_by_address(
                "0x" + "11" * 20, chain_id="all"
            )
        mock.assert_awaited_once_with(address="0x" + "11" * 20, chain_id=None)
        assert result == {"assets": []}

    @pytest.mark.asyncio
    async def test_chain_id_is_parsed_to_int(self):
        mock = AsyncMock(return_value={"assets": []})
        with patch.object(delta_lab.DELTA_LAB_CLIENT, "get_assets_by_address", mock):
            result = await delta_lab.get_assets_by_address(
                "0x" + "11" * 20, chain_id="8453"
            )
        mock.assert_awaited_once_with(address="0x" + "11" * 20, chain_id=8453)
        assert result == {"assets": []}


class TestSearchDeltaLabAssets:
    @pytest.mark.asyncio
    async def test_all_chain_filter_calls_client_with_none(self):
        mock_search = AsyncMock(return_value={"assets": [], "total_count": 0})
        with patch.object(delta_lab.DELTA_LAB_CLIENT, "search_assets", mock_search):
            result = await delta_lab.search_delta_lab_assets("sUSDai", chain="all")
        mock_search.assert_awaited_once_with(query="sUSDai", chain_id=None)
        assert result == {"assets": [], "total_count": 0}

    @pytest.mark.asyncio
    async def test_chain_code_is_mapped_to_chain_id(self):
        mock_search = AsyncMock(
            return_value={"assets": [{"asset_id": 1}], "total_count": 1}
        )
        with patch.object(delta_lab.DELTA_LAB_CLIENT, "search_assets", mock_search):
            result = await delta_lab.search_delta_lab_assets("usdc", chain="base")
        mock_search.assert_awaited_once_with(query="usdc", chain_id=8453)
        assert result["total_count"] == 1

    @pytest.mark.asyncio
    async def test_numeric_chain_id_is_parsed(self):
        mock_search = AsyncMock(return_value={"assets": [], "total_count": 0})
        with patch.object(delta_lab.DELTA_LAB_CLIENT, "search_assets", mock_search):
            result = await delta_lab.search_delta_lab_assets("usdc", chain="8453")
        mock_search.assert_awaited_once_with(query="usdc", chain_id=8453)
        assert result["total_count"] == 0

    @pytest.mark.asyncio
    async def test_unknown_chain_returns_error(self):
        result = await delta_lab.search_delta_lab_assets("usdc", chain="unknown")
        assert result["error"] == "unknown chain filter: 'unknown'"
