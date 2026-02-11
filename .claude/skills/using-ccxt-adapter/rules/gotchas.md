# CCXT adapter gotchas

## Always `await adapter.close()`

Each exchange holds open HTTP sessions. Not closing leaks connections and triggers warnings.

```python
adapter = get_adapter(CCXTAdapter)
try:
    ...
finally:
    await adapter.close()
```

## Hyperliquid uses wallet auth, not API keys

```json
{
  "hyperliquid": {
    "walletAddress": "0x...",
    "privateKey": "0x..."
  }
}
```

Passing `apiKey`/`secret` instead will silently fail on authenticated endpoints.

## Hyperliquid defaults to `swap` (perps)

CCXT's Hyperliquid exchange uses `defaultType: "swap"`. Perp symbols use the `:USDC` suffix:

```python
await adapter.hyperliquid.fetch_ticker("ETH/USDC:USDC")  # perp
```

For spot, set the type per-call or in options:

```python
await adapter.hyperliquid.fetch_ticker("ETH/USDC", params={"type": "spot"})
```

## Symbol format varies by exchange

CCXT unifies most symbols to `BASE/QUOTE` (e.g. `ETH/USDT`), but perp/futures use suffixes:

- Perps: `ETH/USDT:USDT` (linear), `ETH/USD:ETH` (inverse)
- Spot: `ETH/USDT`

Use `exchange.load_markets()` then check `exchange.markets` to see available symbols.

## Rate limits

CCXT handles rate limiting internally when `enableRateLimit` is set (it's off by default). If you're making many calls, consider setting it:

```json
{
  "binance": {
    "apiKey": "...",
    "secret": "...",
    "enableRateLimit": true
  }
}
```

## Exchange instances are properties, not methods

```python
# RIGHT
await adapter.binance.fetch_ticker("BTC/USDT")

# WRONG — no such method on the adapter itself
await adapter.fetch_ticker("binance", "BTC/USDT")
```

## Script execution

Run scripts via MCP with wallet tracking:

```
mcp__wayfinder__run_script(
    script_path=".wayfinder_runs/ccxt_arb.py",
    wallet_label="main"
)
```
