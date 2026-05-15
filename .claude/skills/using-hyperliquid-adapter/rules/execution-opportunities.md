# Hyperliquid execution opportunities (orders/transfers)

## Execution requires an injected executor

`HyperliquidAdapter` execution methods require a `HyperliquidExecutor`:
- Protocol: `wayfinder_paths/core/clients/protocols.py` (`HyperliquidExecutorProtocol`)

If no executor is provided, execution methods raise `NotImplementedError`.

## Minimum order size

**All orders (perp and spot) require a minimum of $10 USD notional.**

Orders below this threshold will be rejected by Hyperliquid.

## High-value execution calls

Orders:
- `place_market_order(asset_id, is_buy, slippage, size, address, reduce_only=False, cloid=None, builder=None)`
- `place_limit_order(asset_id, is_buy, price, size, address, reduce_only=False, builder=None)`
- `place_trigger_order(asset_id, is_buy, trigger_price, size, address, tpsl, is_market=True, limit_price=None)`
  - `tpsl="sl"` → stop-loss; `tpsl="tp"` → take-profit
  - `is_market=True` (default): fires a market order when the trigger price is hit
  - `is_market=False`: fires a limit order at `limit_price` when triggered (requires `limit_price`)
  - Always `reduce_only=True` — closes an existing position, never opens a new one
- `place_stop_loss(asset_id, is_buy, trigger_price, size, address)` — convenience wrapper for `place_trigger_order(..., tpsl="sl", is_market=True)`
- `cancel_order(asset_id, order_id, address)`
- `cancel_order_by_cloid(asset_id, cloid, address)`

Account/risk:
- `update_leverage(asset_id, leverage, is_cross, address)`
- `approve_builder_fee(builder, max_fee_rate, address)`

Transfers:
- `spot_transfer(amount, destination, token, address)`
- `hypercore_to_hyperevm(amount, address, token_address=None)`

Withdrawal:
- `withdraw(amount, address)` (USDC withdraw to Arbitrum via executor)

## Funding the account (deposit pattern)

This repo exposes the Hyperliquid L1 bridge address constant:
- `HYPERLIQUID_BRIDGE_ADDRESS` (Arbitrum destination for USDC deposits)

See also: `rules/deposits-withdrawals.md` for chain, minimum deposit, and expected delays.

Common pattern:
1) Send Arbitrum USDC to the bridge address (ERC20 transfer)
2) Poll for credit using `wait_for_deposit(address, expected_increase)`

Treat this as a **fund-moving operation** and require explicit confirmation.

## Claude Code MCP tools (minimal surface)

For interactive use in Claude Code, this repo exposes a small MCP surface:
- Read-only: `mcp__wayfinder__hyperliquid_get_state` (user state), `mcp__wayfinder__hyperliquid_search_mid_prices`, `mcp__wayfinder__hyperliquid_search_market`
- Writes — each action is its own tool:
  - `mcp__wayfinder__hyperliquid_place_market_order` — IOC market order, perp / spot / HIP-4 outcome
  - `mcp__wayfinder__hyperliquid_place_limit_order` — GTC limit order, perp / spot / HIP-4 outcome (see [outcomes.md](outcomes.md))
  - `mcp__wayfinder__hyperliquid_place_trigger_order` — stop-loss / take-profit, perp only (see below)
  - `mcp__wayfinder__hyperliquid_cancel_order`
  - `mcp__wayfinder__hyperliquid_update_leverage`
  - `mcp__wayfinder__hyperliquid_deposit`
  - `mcp__wayfinder__hyperliquid_withdraw`

### `hyperliquid_place_trigger_order` via MCP

Place a stop-loss or take-profit on a perp position. Closes a position, never opens one — set `is_buy` to the side that closes (long → `False`, short → `True`).

**Required params:**
- `asset_name` — perp market (`"ETH-USDC"`, `"xyz:SP500"`)
- `tpsl` — `"sl"` for stop-loss, `"tp"` for take-profit
- `is_buy` — opposite of your position's side
- `trigger_price` — fires when mark price touches this
- `size` — coin units

**Optional params:**
- `is_market_trigger` (default `True`) — market on touch; `False` needs `price`
- `price` — limit price when `is_market_trigger=False`

**Examples:**

```
# Stop-loss on a long ETH position: close 0.5 ETH if price drops to 2800
hyperliquid_place_trigger_order(
    wallet_label="main",
    asset_name="ETH-USDC",
    tpsl="sl",
    is_buy=False,
    trigger_price=2800.0,
    size=0.5,
)

# Take-profit on a short BTC position: close 0.01 BTC if price drops to 85000
hyperliquid_place_trigger_order(
    wallet_label="main",
    asset_name="BTC-USDC",
    tpsl="tp",
    is_buy=True,
    trigger_price=85000.0,
    size=0.01,
)

# Limit stop-loss (trigger at 2800, fill at 2790 to avoid slippage)
hyperliquid_place_trigger_order(
    wallet_label="main",
    asset_name="ETH-USDC",
    tpsl="sl",
    is_buy=False,
    trigger_price=2800.0,
    size=0.5,
    is_market_trigger=False,
    price=2790.0,
)
```

### Builder fee (“builder code”)

Builder attribution is **mandatory** in this repo:
- Builder wallet: `0xaA1D89f333857eD78F8434CC4f896A9293EFE65c`
- Fee value `f` is measured in **tenths of a basis point** (e.g. `30` → `0.030%`)
- The builder wallet is **fixed**; other addresses are rejected.

Set it in `config.json`:
- `config.json["strategy"]["builder_fee"] = {"b": "0xaA1D89f333857eD78F8434CC4f896A9293EFE65c", "f": 30}`

The `place_market_order` / `place_limit_order` (and trigger) tools will:
- attach the builder config to orders
- auto-approve the builder fee (via `approve_builder_fee`) if needed

### Spot vs perp orders

The place-order tools read the market type from `asset_name` — no `is_spot` flag.

**Perp market order:**
```
hyperliquid_place_market_order(
    wallet_label="main",
    asset_name="HYPE-USDC",
    is_buy=True,
    usd_amount=20,
    usd_amount_kind="notional",
)
```

**Spot market order:**
```
hyperliquid_place_market_order(
    wallet_label="main",
    asset_name="HYPE/USDC",
    is_buy=True,
    usd_amount=11,
)
```

**Perp limit order:**
```
hyperliquid_place_limit_order(
    wallet_label="main",
    asset_name="HYPE-USDC",
    is_buy=True,
    price=12.50,
    size=10,
)
```

Spot orders don't take `usd_amount_kind` (no leverage concept).

### USD sizing (avoid ambiguity)

For perps:
- Use `size` for **coin units** (e.g. ETH, HYPE).
- Or use `usd_amount` + `usd_amount_kind`:
  - `usd_amount_kind="notional"` — position size in USD
  - `usd_amount_kind="margin"` — collateral in USD (requires `leverage`; notional = margin × leverage)

For spot:
- Use `size` for **coin units**, or
- Use `usd_amount` directly (always treated as notional)

## Claude Code "execution mode" (one-off scripts)

If the user wants **immediate execution** (not a reusable strategy), prefer the MCP tools:
- `mcp__wayfinder__hyperliquid_place_market_order` / `_place_limit_order` / `_place_trigger_order` / `_cancel_order` / `_update_leverage` / `_deposit` / `_withdraw`
- `mcp__wayfinder__core_execute` for on-chain transfers (send/swap/deposit)

### `mcp__wayfinder__core_execute` examples

**Send tokens to another address:**
```
mcp__wayfinder__core_execute(
    kind="send",
    wallet_label="main",
    amount="25",
    token="usd-coin-arbitrum",
    recipient="0x112dB0cDc2A111B814138A9b3f93379f49E449F0"
)
```

**Swap tokens:**
```
mcp__wayfinder__core_execute(
    kind="swap",
    wallet_label="main",
    amount="100",
    from_token="usd-coin-arbitrum",
    to_token="ethereum-arbitrum",
    slippage_bps=50
)
```

**Hyperliquid deposit (Bridge2):**
```
mcp__wayfinder__core_execute(
    kind="hyperliquid_deposit",
    wallet_label="main",
    amount="8"
)
```
This hard-codes Arbitrum USDC → `HYPERLIQUID_BRIDGE_ADDRESS`. Follow with `wait_for_deposit(...)` then place the perp order.
