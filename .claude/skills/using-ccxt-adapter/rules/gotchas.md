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

## `get_adapter(CCXTAdapter)` requires `ccxt` section in config.json

If `config.json` has no `ccxt` key, `get_adapter(CCXTAdapter)` loads zero exchanges — no error, but accessing `adapter.aster` raises `AttributeError`. For public-data-only access (no API keys), construct directly:

```python
# No config.json needed — empty dict works for public endpoints (tickers, funding, orderbooks)
adapter = CCXTAdapter(exchanges={"aster": {}, "binance": {}})
```

## For Hyperliquid reads, prefer the SDK Info client over CCXT

The native `HyperliquidAdapter` requires a wallet even for reads. For read-only data (funding history, meta, orderbooks), use the SDK `Info` client directly:

```python
from wayfinder_paths.adapters.hyperliquid_adapter.info import get_info

info = get_info()
meta_and_ctxs = info.meta_and_asset_ctxs()  # sync, not async
rows = info.funding_history("ETH", start_ms, end_ms)  # sync
```

Note: `Info` methods are **sync** (not async). `funding_history` returns `[{"fundingRate": "0.00001", "time": "..."}]`.

## Exchange instances are properties, not methods

```python
# RIGHT
await adapter.binance.fetch_ticker("BTC/USDT")

# WRONG — no such method on the adapter itself
await adapter.fetch_ticker("binance", "BTC/USDT")
```

## Aster-specific quirks

**Min order sizes matter:** BTC min is 0.001 BTC (~$68 at $68k). For small test trades, use ETH (min 0.001 ETH, ~$2). Always check `aster.markets[symbol]["limits"]["amount"]["min"]` before ordering.

**Only USDT-margined perps:** Aster perps are `{COIN}/USDT:USDT`. No `USDC:USDC` pairs available.

**`fetch_balance()` underreports futures margin:** Shows $0 USDT/USDC even when the futures account has funds. Don't gate on balance checks — if the order fails, it fails.

**Market order fills are async:** `create_order` returns `status: "open"` and `filled: 0.0` even for market orders that fill instantly. To confirm execution, call `fetch_positions()` after a short `asyncio.sleep(2)` instead of trusting the order response.

## Bulk `fetch_funding_rate_history` times out if sequential

Fetching funding history for 100+ symbols one-by-one easily exceeds `run_script` timeouts. Use `asyncio.gather` with a semaphore to parallelize:

```python
sem = asyncio.Semaphore(10)

async def fetch_rates(exchange, symbol, since_ms):
    async with sem:
        return await exchange.fetch_funding_rate_history(symbol, since=since_ms, limit=500)

tasks = [fetch_rates(adapter.aster, f"{coin}/USDT:USDT", since_ms) for coin in coins]
results = await asyncio.gather(*tasks)
```

Don't combine `enableRateLimit: true` with your own semaphore — it double-throttles and makes timeouts worse. Pick one concurrency control mechanism.

## Discovering perp markets programmatically

Call `load_markets()` first, then filter on market properties:

```python
await adapter.aster.load_markets()
perp_coins = {
    m["base"]
    for m in adapter.aster.markets.values()
    if m.get("swap") and m.get("linear") and m.get("quote") == "USDT"
}
```

## Funding settlement intervals differ across exchanges

Aster settles every 8h, Hyperliquid every 1h. When comparing rates, annualize with the correct multiplier:

```python
aster_annual = avg_8h_rate * 3 * 365    # 3 settlements/day
hl_annual    = avg_1h_rate * 24 * 365   # 24 settlements/day
```

Raw rate numbers are not comparable without normalization.

## Script execution

Run scripts via MCP with wallet tracking:

```
mcp__wayfinder__run_script(
    script_path=".wayfinder_runs/ccxt_arb.py",
    wallet_label="main"
)
```
