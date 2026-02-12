# CCXT adapter examples

## Fetch a ticker

```python
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.ccxt_adapter import CCXTAdapter

async def main():
    adapter = get_adapter(CCXTAdapter)
    ticker = await adapter.binance.fetch_ticker("BTC/USDT")
    print(f"BTC/USDT last: {ticker['last']}")
    await adapter.close()

asyncio.run(main())
```

## Fetch balances

```python
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.ccxt_adapter import CCXTAdapter

async def main():
    adapter = get_adapter(CCXTAdapter)
    balance = await adapter.binance.fetch_balance()
    print(f"USDT free: {balance['USDT']['free']}")
    await adapter.close()

asyncio.run(main())
```

## Place a market order

```python
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.ccxt_adapter import CCXTAdapter

async def main():
    adapter = get_adapter(CCXTAdapter)
    order = await adapter.binance.create_order("ETH/USDT", "market", "buy", 0.01)
    print(f"Order: {order['id']} status={order['status']}")
    await adapter.close()

asyncio.run(main())
```

## Place a limit order

```python
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.ccxt_adapter import CCXTAdapter

async def main():
    adapter = get_adapter(CCXTAdapter)
    order = await adapter.aster.create_order("ETH/USDT", "limit", "buy", 0.01, 2000.0)
    print(f"Order: {order['id']} status={order['status']}")
    await adapter.close()

asyncio.run(main())
```

## Fetch orderbook

```python
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.ccxt_adapter import CCXTAdapter

async def main():
    adapter = get_adapter(CCXTAdapter)
    ob = await adapter.binance.fetch_order_book("ETH/USDT", limit=5)
    print(f"Best bid: {ob['bids'][0]}, Best ask: {ob['asks'][0]}")
    await adapter.close()

asyncio.run(main())
```

## Multi-exchange comparison

```python
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.ccxt_adapter import CCXTAdapter

async def main():
    adapter = get_adapter(CCXTAdapter)

    binance_ticker = await adapter.binance.fetch_ticker("ETH/USDT")
    aster_ticker = await adapter.aster.fetch_ticker("ETH/USDT")

    spread = aster_ticker["last"] - binance_ticker["last"]
    print(f"Binance: {binance_ticker['last']}")
    print(f"Aster:   {aster_ticker['last']}")
    print(f"Spread:  {spread:.2f}")

    await adapter.close()

asyncio.run(main())
```

## Hyperliquid via CCXT

```python
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.ccxt_adapter import CCXTAdapter

async def main():
    adapter = get_adapter(CCXTAdapter)

    # Hyperliquid defaults to swap (perp) markets
    ticker = await adapter.hyperliquid.fetch_ticker("ETH/USDC:USDC")
    print(f"ETH perp: {ticker['last']}")

    positions = await adapter.hyperliquid.fetch_positions()
    for p in positions:
        if float(p["contracts"]) != 0:
            print(f"  {p['symbol']} size={p['contracts']} pnl={p['unrealizedPnl']}")

    await adapter.close()

asyncio.run(main())
```

## Key CCXT methods reference

Full API: https://docs.ccxt.com/#/README?id=unified-api

| Method | Purpose |
|--------|---------|
| `fetch_ticker(symbol)` | Last price, bid/ask, volume |
| `fetch_tickers(symbols?)` | Multiple tickers at once |
| `fetch_order_book(symbol, limit?)` | Bids and asks |
| `fetch_ohlcv(symbol, timeframe, since?, limit?)` | Candles |
| `fetch_balance()` | All balances (free, used, total) |
| `create_order(symbol, type, side, amount, price?)` | Place order |
| `cancel_order(id, symbol)` | Cancel order |
| `fetch_open_orders(symbol?)` | Open orders |
| `fetch_positions(symbols?)` | Perp positions |
| `fetch_my_trades(symbol, since?, limit?)` | Trade history |
