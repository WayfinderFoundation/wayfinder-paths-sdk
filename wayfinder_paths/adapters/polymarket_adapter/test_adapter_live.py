import os

import pytest

from wayfinder_paths.adapters.polymarket_adapter.adapter import PolymarketAdapter
from wayfinder_paths.mcp.scripting import get_adapter

if os.getenv("RUN_POLYMARKET_LIVE_TESTS", "").lower() not in ("1", "true", "yes"):
    pytest.skip(
        "Polymarket live tests are disabled (set RUN_POLYMARKET_LIVE_TESTS=1 to enable).",
        allow_module_level=True,
    )


@pytest.fixture
async def live_adapter():
    wallet_label = os.getenv("POLYMARKET_WALLET_LABEL", "main")
    adapter = await get_adapter(PolymarketAdapter, wallet_label)
    try:
        yield adapter
    finally:
        await adapter.close()


class TestPolymarketLiveRead:
    @pytest.mark.asyncio
    async def test_search_and_market_data(self, live_adapter):
        ok, markets = await live_adapter.search_markets(query="super bowl", limit=10)
        assert ok
        assert isinstance(markets, list)
        assert len(markets) > 0

        market = next((m for m in markets if m.get("yesTokenId")), markets[0])
        token_id = str(market["yesTokenId"])
        assert token_id, "Expected yesTokenId on at least one market"

        ok, price = await live_adapter.get_price(token_id=token_id, side="BUY")
        assert ok
        assert isinstance(price, dict)
        assert "price" in price

        ok, hist = await live_adapter.get_prices_history(
            token_id=token_id, interval="1d", fidelity=5
        )
        assert ok
        assert isinstance(hist, dict)
        assert "history" in hist
