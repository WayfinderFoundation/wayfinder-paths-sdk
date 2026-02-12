# Pendle Adapter

Adapter for Pendle API + Hosted SDK endpoints to support:

- Market discovery (PT/YT markets, APYs, liquidity/volume/expiry filtering)
- Historical metrics (per-market time series)
- Execution planning (swap quote → ready-to-send `tx` + required `tokenApprovals`)

## Capabilities

- `pendle.markets.read`: Fetch whitelisted markets (`/v1/markets/all`)
- `pendle.market.snapshot`: Fetch a market snapshot (`/v2/{chainId}/markets/{market}/data`)
- `pendle.market.history`: Fetch market historical data (`/v2/{chainId}/markets/{market}/historical-data`)
- `pendle.prices.ohlcv`: Fetch token OHLCV (`/v4/{chainId}/prices/{token}/ohlcv`)
- `pendle.prices.assets`: Fetch all asset prices (`/v1/prices/assets`)
- `pendle.swap.quote`: Build Hosted SDK swap payload (`/v2/sdk/{chainId}/markets/{market}/swap`)
- `pendle.swap.best_pt`: Select and quote “best” PT swap on a chain
- `pendle.convert.quote`: Universal Hosted SDK convert quote (`/v2/sdk/{chainId}/convert`)
- `pendle.convert.best_pt`: Select and quote “best” PT via convert endpoint
- `pendle.convert.execute`: Broadcast Hosted SDK convert tx (incl approvals)
- `pendle.positions.database`: Indexed positions snapshot (`/v1/dashboard/positions/database/{user}`; claimables cached)
- `pendle.limit_orders.*`: Limit order discovery + maker APIs (`/v1/limit-orders/...`)
- `pendle.deployments.read`: Load Pendle core deployments JSON (router/routerStatic/limitRouter)
- `pendle.router_static.rates`: Off-chain spot-rate sanity checks via RouterStatic contract

## Configuration

- `PENDLE_API_URL` (env var): defaults to `https://api-v2.pendle.finance/core`
- Optional config:
  - `config["pendle_adapter"]["base_url"]`
  - `config["pendle_adapter"]["timeout"]`
  - `config["pendle_adapter"]["deployments_base_url"]` (defaults to Pendle’s public core deployments on GitHub)
  - `config["pendle_adapter"]["max_retries"]`, `retry_backoff_seconds`

## Usage

### List active PT/YT markets (multi-chain)

```python
from adapters.pendle_adapter.adapter import PendleAdapter

adapter = PendleAdapter()

rows = await adapter.list_active_pt_yt_markets(
    chains=["ethereum", "arbitrum", "base", "hyperevm", "plasma"],
    min_liquidity_usd=250_000,
    min_volume_usd_24h=25_000,
    min_days_to_expiry=7,
    sort_by="fixed_apy",
    descending=True,
)
```

### Build the best PT swap transaction (single chain)

```python
best = await adapter.build_best_pt_swap_tx(
    chain="arbitrum",
    token_in="0xaf88d065e77c8cC2239327C5EDb3A432268e5831",  # example: USDC (Arbitrum)
    amount_in=str(1000 * 10**6),  # 1000 USDC, base units (6 decimals)
    receiver="0xYourEOAHere",
    slippage=0.01,
    enable_aggregator=True,
)

if best["ok"]:
    tx = best["tx"]
    approvals = best["tokenApprovals"]
```

### Build a universal convert transaction (token -> PT)

```python
convert = await adapter.sdk_convert_v2(
    chain="arbitrum",
    slippage=0.01,
    receiver="0xYourEOAHere",
    inputs=[{"token": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", "amount": str(1 * 10**6)}],  # 1 USDC
    outputs=["0x97c1a4ae3e0da8009aff13e3e3ee7ea5ee4afe84"],  # PT token address
    enable_aggregator=True,
    aggregators=["kyberswap"],
    additional_data=["impliedApy", "effectiveApy", "priceImpact"],
)
plan = adapter.build_convert_plan(chain="arbitrum", convert_response=convert)
```

### Execute a universal convert (handles approvals + broadcast)

```python
ok, res = await adapter.execute_convert(
    chain="arbitrum",
    slippage=0.01,
    inputs=[{"token": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", "amount": str(1 * 10**6)}],
    outputs=["0x97c1a4ae3e0da8009aff13e3e3ee7ea5ee4afe84"],
)
```

## Notes

- “Fixed APY” proxy is `details.impliedApy` from `/v1/markets/all`.
- `build_best_pt_swap_tx()` requests Hosted SDK `additionalData=impliedApy,effectiveApy` and prefers `effectiveApy` when present.
- All Pendle REST/SDK responses include a `rateLimit` field populated from headers (x-ratelimit-* and x-computing-unit) for CU-aware budgeting.
