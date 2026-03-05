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

    @pytest.mark.asyncio
    async def test_chain_code_is_mapped_to_chain_id(self):
        mock = AsyncMock(return_value={"assets": []})
        with patch.object(delta_lab.DELTA_LAB_CLIENT, "get_assets_by_address", mock):
            result = await delta_lab.get_assets_by_address(
                "0x" + "11" * 20, chain_id="base"
            )
        mock.assert_awaited_once_with(address="0x" + "11" * 20, chain_id=8453)
        assert result == {"assets": []}

    @pytest.mark.asyncio
    async def test_unknown_chain_returns_error(self):
        result = await delta_lab.get_assets_by_address(
            "0x" + "11" * 20, chain_id="unknown"
        )
        assert result["error"] == "unknown chain filter: 'unknown'"


class TestSearchDeltaLabAssets:
    @pytest.mark.asyncio
    async def test_calls_client_search(self):
        mock = AsyncMock(return_value={"assets": [], "total_count": 0})
        with patch.object(delta_lab.DELTA_LAB_CLIENT, "search_assets", mock):
            result = await delta_lab.search_delta_lab_assets("sUSDai")
        mock.assert_awaited_once_with(query="sUSDai", chain_id=None, limit=25)
        assert result == {"assets": [], "total_count": 0}

    @pytest.mark.asyncio
    async def test_chain_code_is_mapped_to_chain_id(self):
        mock = AsyncMock(return_value={"assets": [], "total_count": 0})
        with patch.object(delta_lab.DELTA_LAB_CLIENT, "search_assets", mock):
            result = await delta_lab.search_delta_lab_assets("usdc", chain="base")
        mock.assert_awaited_once_with(query="usdc", chain_id=8453, limit=25)
        assert result["total_count"] == 0

    @pytest.mark.asyncio
    async def test_limit_is_parsed(self):
        mock = AsyncMock(return_value={"assets": [], "total_count": 0})
        with patch.object(delta_lab.DELTA_LAB_CLIENT, "search_assets", mock):
            result = await delta_lab.search_delta_lab_assets(
                "usdc", chain="all", limit="10"
            )
        mock.assert_awaited_once_with(query="usdc", chain_id=None, limit=10)
        assert result["total_count"] == 0

    @pytest.mark.asyncio
    async def test_unknown_chain_returns_error(self):
        result = await delta_lab.search_delta_lab_assets("usdc", chain="unknown")
        assert result["error"] == "unknown chain filter: 'unknown'"


class TestScreenBorrowRoutes:
    @pytest.mark.asyncio
    async def test_chain_code_is_mapped_to_chain_id(self):
        mock = AsyncMock(return_value={"data": [], "count": 0})
        with patch.object(delta_lab.DELTA_LAB_CLIENT, "screen_borrow_routes", mock):
            result = await delta_lab.screen_borrow_routes(chain_id="base")
        mock.assert_awaited_once()
        assert mock.call_args.kwargs["chain_id"] == 8453
        assert result == {"data": [], "count": 0}

    @pytest.mark.asyncio
    async def test_unknown_chain_returns_error(self):
        result = await delta_lab.screen_borrow_routes(chain_id="unknown")
        assert result["error"] == "unknown chain filter: 'unknown'"


class TestScreenPriceByAssetIds:
    @pytest.mark.asyncio
    async def test_calls_client_with_asset_ids(self):
        mock = AsyncMock(return_value={"data": [], "count": 0})
        with patch.object(delta_lab.DELTA_LAB_CLIENT, "screen_price", mock):
            result = await delta_lab.screen_price_by_asset_ids(
                asset_ids="1,2", limit="10"
            )
        mock.assert_awaited_once()
        assert mock.call_args.kwargs["asset_ids"] == [1, 2]
        assert mock.call_args.kwargs["limit"] == 10
        assert result == {"data": [], "count": 0}


class TestScreenLendingByAssetIds:
    @pytest.mark.asyncio
    async def test_calls_client_with_asset_ids(self):
        mock = AsyncMock(return_value={"data": [], "count": 0})
        with patch.object(delta_lab.DELTA_LAB_CLIENT, "screen_lending", mock):
            result = await delta_lab.screen_lending_by_asset_ids(
                asset_ids="1,2", limit="10"
            )
        mock.assert_awaited_once()
        assert mock.call_args.kwargs["asset_ids"] == [1, 2]
        assert mock.call_args.kwargs["exclude_frozen"] is True
        assert mock.call_args.kwargs["limit"] == 10
        assert result == {"data": [], "count": 0}


class TestScreenPerpByAssetIds:
    @pytest.mark.asyncio
    async def test_calls_client_with_asset_ids(self):
        mock = AsyncMock(return_value={"data": [], "count": 0})
        with patch.object(delta_lab.DELTA_LAB_CLIENT, "screen_perp", mock):
            result = await delta_lab.screen_perp_by_asset_ids(
                asset_ids="1,2", limit="10"
            )
        mock.assert_awaited_once()
        assert mock.call_args.kwargs["asset_ids"] == [1, 2]
        assert mock.call_args.kwargs["limit"] == 10
        assert result == {"data": [], "count": 0}


class TestScreenBorrowRoutesByAssetIds:
    @pytest.mark.asyncio
    async def test_calls_client_with_asset_ids_and_chain_filter(self):
        mock = AsyncMock(return_value={"data": [], "count": 0})
        with patch.object(delta_lab.DELTA_LAB_CLIENT, "screen_borrow_routes", mock):
            result = await delta_lab.screen_borrow_routes_by_asset_ids(
                asset_ids="1,2",
                borrow_asset_ids="3",
                chain_id="base",
                limit="10",
            )
        mock.assert_awaited_once()
        assert mock.call_args.kwargs["asset_ids"] == [1, 2]
        assert mock.call_args.kwargs["borrow_asset_ids"] == [3]
        assert mock.call_args.kwargs["chain_id"] == 8453
        assert mock.call_args.kwargs["limit"] == 10
        assert result == {"data": [], "count": 0}

    @pytest.mark.asyncio
    async def test_unknown_chain_returns_error(self):
        result = await delta_lab.screen_borrow_routes_by_asset_ids(chain_id="unknown")
        assert result["error"] == "unknown chain filter: 'unknown'"
