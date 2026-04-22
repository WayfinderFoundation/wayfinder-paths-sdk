import os
import random
from decimal import Decimal, InvalidOperation
from typing import Any

import pytest
import pytest_asyncio

from wayfinder_paths.adapters.polymarket_adapter.adapter import PolymarketAdapter
from wayfinder_paths.mcp.scripting import get_adapter

if os.getenv("RUN_POLYMARKET_LIVE_TESTS", "").lower() not in ("1", "true", "yes"):
    pytest.skip(
        "Polymarket live tests are disabled (set RUN_POLYMARKET_LIVE_TESTS=1 to enable).",
        allow_module_level=True,
    )


RUN_POLYMARKET_EXECUTE_LIVE_TESTS = os.getenv(
    "RUN_POLYMARKET_EXECUTE_LIVE_TESTS", ""
).lower() in ("1", "true", "yes")
POLYMARKET_LIVE_QUERY = os.getenv("POLYMARKET_LIVE_QUERY", "").strip()
POLYMARKET_LIVE_OUTCOME = os.getenv("POLYMARKET_LIVE_OUTCOME", "YES").strip() or "YES"
POLYMARKET_LIVE_RANDOM_SEED = int(os.getenv("POLYMARKET_LIVE_RANDOM_SEED", "7"))


def _decimal_or_none(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _book_best_ask(book: dict[str, Any]) -> Decimal | None:
    asks = book.get("asks") or []
    prices = [
        price
        for level in asks
        if isinstance(level, dict)
        and (price := _decimal_or_none(level.get("price"))) is not None
        and price > 0
    ]
    return min(prices) if prices else None


def _book_best_bid(book: dict[str, Any]) -> Decimal | None:
    bids = book.get("bids") or []
    prices = [
        price
        for level in bids
        if isinstance(level, dict)
        and (price := _decimal_or_none(level.get("price"))) is not None
        and price > 0
    ]
    return max(prices) if prices else None


def _book_tick_size(book: dict[str, Any]) -> Decimal:
    tick_size = _decimal_or_none(book.get("tick_size"))
    return tick_size if tick_size is not None and tick_size > 0 else Decimal("0.01")


def _book_min_order_shares(book: dict[str, Any]) -> Decimal:
    min_size = _decimal_or_none(book.get("min_order_size"))
    return min_size if min_size is not None and min_size > 0 else Decimal("1")


def _derive_resting_limit_buy_price(book: dict[str, Any]) -> Decimal:
    tick_size = _book_tick_size(book)
    best_ask = _book_best_ask(book)
    if best_ask is not None:
        return max(tick_size, best_ask - tick_size)

    best_bid = _book_best_bid(book)
    if best_bid is not None:
        return max(tick_size, best_bid)

    return tick_size


def _extract_order_id(payload: dict[str, Any]) -> str | None:
    for key in ("orderID", "orderId", "order_id", "id"):
        value = payload.get(key)
        if value is None:
            continue
        order_id = str(value).strip()
        if order_id:
            return order_id

    order_data = payload.get("order")
    if isinstance(order_data, dict):
        for key in ("orderID", "orderId", "order_id", "id"):
            value = order_data.get(key)
            if value is None:
                continue
            order_id = str(value).strip()
            if order_id:
                return order_id

    return None


async def _pick_market_for_resting_limit_buy(
    adapter: PolymarketAdapter,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    if POLYMARKET_LIVE_QUERY:
        ok, markets = await adapter.search_markets_fuzzy(
            query=POLYMARKET_LIVE_QUERY,
            limit=30,
        )
    else:
        ok, markets = await adapter.list_markets(
            closed=False,
            limit=60,
            order="volume24hr",
            ascending=False,
        )
    assert ok, f"Failed to load candidate markets: {markets}"
    assert isinstance(markets, list) and markets, "No candidate markets found"

    candidates = [
        market
        for market in markets
        if market.get("enableOrderBook")
        and market.get("clobTokenIds")
        and market.get("closed") is not True
        and market.get("active") is not False
    ]
    assert candidates, "No tradable markets found in live search results"

    if not POLYMARKET_LIVE_QUERY:
        rng = random.Random(POLYMARKET_LIVE_RANDOM_SEED)
        rng.shuffle(candidates)

    for market in candidates:
        ok_tid, token_id = adapter.resolve_clob_token_id(
            market=market,
            outcome=POLYMARKET_LIVE_OUTCOME,
        )
        if not ok_tid:
            ok_tid, token_id = adapter.resolve_clob_token_id(market=market, outcome=0)
            if not ok_tid:
                continue

        ok_book, book = await adapter.get_order_book(token_id=token_id)
        if ok_book and isinstance(book, dict):
            return market, token_id, book

    raise AssertionError("No tradable market had an order book")


async def _pick_cancelable_open_order(
    adapter: PolymarketAdapter,
) -> tuple[dict[str, Any], str]:
    ok_orders, open_orders = await adapter.list_open_orders()
    assert ok_orders, f"Failed to fetch open orders: {open_orders}"
    assert isinstance(open_orders, list)

    for order in open_orders:
        if not isinstance(order, dict):
            continue
        order_id = _extract_order_id(order)
        if order_id:
            return order, order_id

    pytest.skip("No open order found to cancel.")


@pytest_asyncio.fixture
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
        ok, markets = await live_adapter.search_markets_fuzzy(
            query="super bowl", limit=10
        )
        assert ok
        assert isinstance(markets, list)
        assert len(markets) > 0

        market = next(
            (
                m
                for m in markets
                if m.get("enableOrderBook")
                and live_adapter._ensure_list(m.get("clobTokenIds"))
            ),
            markets[0],
        )

        token_ids = live_adapter._ensure_list(market.get("clobTokenIds"))
        assert token_ids, "Expected clobTokenIds on at least one market"

        token_id = str(token_ids[0])
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


class TestPolymarketLiveExecute:
    @pytest.mark.asyncio
    @pytest.mark.skipif(
        not RUN_POLYMARKET_EXECUTE_LIVE_TESTS,
        reason=(
            "Live order execution is disabled "
            "(set RUN_POLYMARKET_EXECUTE_LIVE_TESTS=1 to enable)."
        ),
    )
    async def test_place_limit_buy_only(self, live_adapter: PolymarketAdapter):
        market, token_id, book = await _pick_market_for_resting_limit_buy(live_adapter)

        min_size = _book_min_order_shares(book)
        resting_price = _derive_resting_limit_buy_price(book)
        if resting_price >= Decimal("1"):
            pytest.skip(
                f"Could not derive a valid resting bid for {market.get('slug')}."
            )

        ok_place, placed = await live_adapter.place_limit_order(
            token_id=token_id,
            side="BUY",
            price=float(resting_price),
            size=float(min_size),
            post_only=True,
        )
        assert ok_place, f"Limit BUY failed for market {market.get('slug')}: {placed}"
        assert isinstance(placed, dict)

        order_id = _extract_order_id(placed)
        assert order_id, (
            f"Could not find order id in place_limit_order response: {placed}"
        )

        ok_orders, open_orders = await live_adapter.list_open_orders(token_id=token_id)
        assert ok_orders, f"Failed to fetch open orders after placement: {open_orders}"
        assert isinstance(open_orders, list)
        assert any(
            _extract_order_id(order) == order_id
            for order in open_orders
            if isinstance(order, dict)
        ), f"Placed order {order_id} not found in open orders: {open_orders}"

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        not RUN_POLYMARKET_EXECUTE_LIVE_TESTS,
        reason=(
            "Live order execution is disabled "
            "(set RUN_POLYMARKET_EXECUTE_LIVE_TESTS=1 to enable)."
        ),
    )
    async def test_cancel_existing_order_only(self, live_adapter: PolymarketAdapter):
        order, order_id = await _pick_cancelable_open_order(live_adapter)

        ok_cancel, canceled = await live_adapter.cancel_order(order_id=order_id)
        assert ok_cancel, f"Cancel failed for order {order_id}: {canceled}"

        ok_orders, open_orders = await live_adapter.list_open_orders()
        assert ok_orders, f"Failed to fetch open orders after cancel: {open_orders}"
        assert isinstance(open_orders, list)
        assert all(
            _extract_order_id(existing_order) != order_id
            for existing_order in open_orders
            if isinstance(existing_order, dict)
        ), (
            f"Canceled order {order_id} still appears in open orders: "
            f"{order} | {open_orders}"
        )
