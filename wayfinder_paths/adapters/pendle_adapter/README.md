# Pendle Adapter

Adapter for Pendle API + Hosted SDK endpoints to support:

- Market discovery (PT/YT markets, APYs, liquidity/volume/expiry filtering)
- Historical metrics (per-market time series)
- Execution planning (swap quote → ready-to-send `tx` + required `tokenApprovals`)

## Capabilities

- `pendle.markets.read`: Fetch whitelisted markets (`/v2/markets/all`)
- `pendle.market.snapshot`: Fetch a market snapshot (`/v2/{chainId}/markets/{market}/data`)
- `pendle.market.history`: Fetch market historical data (`/v3/{chainId}/markets/{market}/historical-data`)
- `pendle.prices.ohlcv`: Fetch token OHLCV (`/v4/{chainId}/prices/{token}/ohlcv`)
- `pendle.prices.assets`: Fetch all asset prices (`/v1/prices/assets`)
- `pendle.swap.quote`: Build Hosted SDK swap payload (`/v2/sdk/{chainId}/markets/{market}/swap`)
- `pendle.swap.best_pt`: Select and quote “best” PT swap on a chain
- `pendle.convert.quote`: Universal Hosted SDK convert quote (`/v2/sdk/{chainId}/convert`)
- `pendle.convert.best_pt`: Select and quote “best” PT via convert endpoint
- `pendle.convert.execute`: Broadcast Hosted SDK convert tx (incl approvals)
- `pendle.positions.database`: Indexed positions snapshot (`/v1/dashboard/positions/database/{user}`; claimables cached)
- `pendle.limit_orders.*`: Taker liquidity, taker fills, maker create/cancel (`/v1/takers/...`, `/v1/makers/...`, LimitRouter)
- `pendle.deployments.read`: Load Pendle core deployments JSON (router/routerStatic/limitRouter)
- `pendle.router_static.rates`: Off-chain spot-rate sanity checks via RouterStatic contract

## Configuration

- `PENDLE_API_URL` (env var): defaults to `https://api-v2.pendle.finance/core`
- Optional config:
  - `config["pendle_adapter"]["base_url"]`
  - `config["pendle_adapter"]["limit_order_base_url"]` (defaults to `https://api-v2.pendle.finance/limit-order`)
  - `config["pendle_adapter"]["timeout"]`
  - `config["pendle_adapter"]["deployments_base_url"]` (defaults to Pendle’s public core deployments on GitHub)
  - `config["pendle_adapter"]["max_retries"]`, `retry_backoff_seconds`
  - `config["pendle_adapter"]["user_agent"]` (defaults to `wayfinder-paths-sdk/pendle-adapter`)

The adapter sends a default `User-Agent`. Pendle's API may reject some raw
HTTP clients without one.

For ad-hoc Pendle endpoints that do not yet have a typed adapter method, use
the exported helpers instead of raw `urllib`/manual `httpx`:

```python
from wayfinder_paths.adapters.pendle_adapter import pendle_api_get

markets = await pendle_api_get(
    "/v2/markets/all",
    params={"chainId": 42161, "isActive": "true", "limit": 10},
)

orders = await pendle_api_get(
    "/v1/takers/limit-orders",
    api="limit_order",
    params={
        "chainId": 42161,
        "yt": "0x...",
        "type": 0,
        "sortBy": "Implied Rate",
        "sortOrder": "asc",
    },
)
```

These helpers share the adapter's User-Agent, retries, JSON decoding, and
rate-limit metadata handling.

## Usage

### get_full_user_state (positions snapshot)

Fetch a user’s Pendle positions across supported chains (wrapper) or on a single chain.

The per-chain snapshot (`get_full_user_state_per_chain`) is an **on-chain ERC20 balance scan** via Multicall:
- fetch markets from Pendle API (market/pt/yt/sy addresses + expiry metadata)
- multicall `balanceOf(account)` (+ `decimals()`) for PT/YT/LP/(SY)
- optional `marketSnapshot` enrichment via Pendle API when `include_prices=True`

```python
from wayfinder_paths.adapters.pendle_adapter import PendleAdapter

adapter = PendleAdapter()

# Single chain
ok, state = await adapter.get_full_user_state_per_chain(
    chain="arbitrum",
    account="0x...",
    include_zero_positions=False,
    include_prices=True,
)

# All supported chains
ok, state = await adapter.get_full_user_state(account="0x...", include_prices=False)
```

The per-chain result includes: `protocol`, `source`, `chainId`, `account`, `positions`.

### List active PT/YT markets (multi-chain)

```python
from wayfinder_paths.adapters.pendle_adapter import PendleAdapter

adapter = PendleAdapter()

rows = await adapter.list_active_pt_yt_markets(
    chains=["ethereum", "arbitrum", "base", "sonic", "hyperevm", "plasma"],
    min_liquidity_usd=250_000,
    min_volume_usd_24h=25_000,
    min_days_to_expiry=7,
    sort_by="fixed_apy",
    descending=True,
)
```

`list_active_pt_yt_markets()` uses the current `GET /v2/markets/all` endpoint
and paginates through active markets. It returns normalized rows where
`fixedApy` is Pendle `details.impliedApy` and `underlyingApy` is
`details.underlyingApy`; these are decimal APYs, so `0.12` is `12%`.

### Find active Arbitrum stablecoin PT markets

```python
rows = await adapter.list_active_pt_yt_markets(
    chains=["arbitrum"],
    min_liquidity_usd=250_000,
    min_days_to_expiry=7,
    sort_by="fixed_apy",
    descending=True,
)

stable_rows = [
    row
    for row in rows
    if any(symbol in row["marketName"].upper() for symbol in ["USD", "DAI", "USDE"])
]

for row in stable_rows[:5]:
    print(
        row["marketName"],
        f"{row['fixedApy'] * 100:.2f}%",
        row["expiry"],
        row["marketAddress"],
        row["ptAddress"],
    )
```

### Fetch taker limit-order liquidity

Pendle's docs name order types like `TOKEN_FOR_PT`; the REST API encodes them
as integer values. The adapter accepts either form and defaults the currently
required sort fields (`Implied Rate`, `asc`).

```python
orders = await adapter.fetch_taker_limit_orders(
    chain="plasma",
    yt="0x...",
    order_type="TOKEN_FOR_PT",
    skip=0,
    limit=10,
)
```

### Fill an existing taker limit order

`fetch_taker_limit_orders()` returns signed maker orders plus taker-side
`makingAmount`, `netFromTaker`, and `netToTaker`. The fill helper uses
Pendle's `LimitRouter.fill(...)`, approves the taker input token, and defaults
`maxTaking` to `netFromTaker * 1.01`.

```python
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.pendle_adapter import PendleAdapter

adapter = await get_adapter(PendleAdapter, "main")

page = await adapter.fetch_taker_limit_orders(
    chain="arbitrum",
    yt="0x...",
    order_type="TOKEN_FOR_PT",
    limit=1,
)

ok, result = await adapter.execute_taker_limit_order_fill(
    chain="arbitrum",
    limit_order_items=page["results"][0],
    max_taking_bps=100,
)
```

### Create or cancel maker limit orders

Maker creation is a signed off-chain order flow:
1. `generate_maker_limit_order_data(...)` asks Pendle for salt/nonce/rates.
2. `create_maker_limit_order(...)` signs EIP-712 and posts to `/v1/makers/limit-orders`.
3. The maker must have live-chain balance/allowance for the offered token.
   For `PT_FOR_TOKEN`, pass `approval_token=<PT address>` because Pendle's
   generate response only carries the YT address.

```python
ok, result = await adapter.create_maker_limit_order(
    chain="arbitrum",
    yt="0x...",
    order_type="TOKEN_FOR_PT",
    token="0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
    making_amount=str(100 * 10**6),
    implied_apy=0.10,
    expiry=str(1_893_456_000),
)

ok, cancel = await adapter.cancel_maker_limit_order(
    chain="arbitrum",
    limit_order_items=result["order"],
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

Use `enable_aggregator=True` when the input token is not already a valid
Pendle SY input for the selected market. For example, arbitrary USDC -> PT
routes commonly need aggregator routing into the underlying/SY path.

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

- Market discovery uses `GET /v2/markets/all` with pagination. The raw endpoint
  returns `results`; the adapter normalizes this to `markets` for compatibility.
- Market history uses `GET /v3/{chainId}/markets/{market}/historical-data`.
  Pendle documents v3 as backward-compatible with v2 and adds optional APY/fee
  breakdown flags.
- “Fixed APY” proxy is `details.impliedApy` from `/v2/markets/all`.
- Pendle docs recommend `POST /v3/sdk/{chainId}/convert` for new integrations.
  This adapter currently uses the v2 convert endpoint for compatibility.
- Taker limit-order fills are gorlami-testable against forked live orders. Maker
  order creation is unit-tested only by default because Pendle's API validates
  maker balance/allowance on the live production chain, not the gorlami fork.
- `build_best_pt_swap_tx()` requests Hosted SDK `additionalData=impliedApy,effectiveApy` and prefers `effectiveApy` when present.
- All Pendle REST/SDK responses include a `rateLimit` field populated from headers (x-ratelimit-* and x-computing-unit) for CU-aware budgeting.
