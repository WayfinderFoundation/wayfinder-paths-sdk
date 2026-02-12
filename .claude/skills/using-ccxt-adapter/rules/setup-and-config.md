# CCXT adapter setup and config

## What it is

A multi-exchange factory adapter. Each exchange you configure becomes a property on the adapter instance (`adapter.binance`, `adapter.hyperliquid`, etc.). No proxied methods — you call CCXT's API directly on each exchange object.

- Adapter source: `wayfinder_paths/adapters/ccxt_adapter/adapter.py`
- CCXT docs: https://docs.ccxt.com/
- CCXT exchange list: https://github.com/ccxt/ccxt/wiki/Exchange-Markets

## Init patterns

### Config-driven (via `config.json`)

```python
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.ccxt_adapter import CCXTAdapter

adapter = get_adapter(CCXTAdapter)
ticker = await adapter.binance.fetch_ticker("BTC/USDT")
await adapter.close()
```

Reads from the `ccxt` section of `config.json`:

```json
{
  "ccxt": {
    "binance": {
      "apiKey": "...",
      "secret": "..."
    },
    "hyperliquid": {
      "walletAddress": "0x...",
      "privateKey": "0x..."
    },
    "aster": {
      "apiKey": "...",
      "secret": "..."
    }
  }
}
```

### Explicit exchanges kwarg

```python
from wayfinder_paths.adapters.ccxt_adapter import CCXTAdapter

adapter = CCXTAdapter(exchanges={
    "binance": {"apiKey": "...", "secret": "..."},
    "bybit": {},
})
```

The `exchanges` kwarg takes priority over `config["ccxt"]`.

## Credentials per exchange

Each exchange has its own credential format. See the exchange's `describe()` method or the CCXT docs for accepted params: https://docs.ccxt.com/#/README?id=exchange-structure

| Exchange | Required params |
|----------|----------------|
| binance | `apiKey`, `secret` |
| hyperliquid | `walletAddress`, `privateKey` |
| aster | `apiKey`, `secret` |
| bybit | `apiKey`, `secret` |
| dydx | `apiKey`, `secret`, `password` |

All opts are passed straight through to the CCXT constructor, so any param CCXT accepts (e.g. `options`, `password`, `uid`) works.
