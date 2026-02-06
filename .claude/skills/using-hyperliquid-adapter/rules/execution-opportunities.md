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
- `place_stop_loss(asset_id, is_buy, trigger_price, size, address)`
- `cancel_order(asset_id, order_id, address)`
- `cancel_order_by_cloid(asset_id, cloid, address)`

Account/risk:
- `update_leverage(asset_id, leverage, is_cross, address)`
- `approve_builder_fee(builder, max_fee_rate, address)`

Transfers:
- `transfer_spot_to_perp(amount, address)` / `transfer_perp_to_spot(amount, address)`
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
- Read-only: `mcp__wayfinder__hyperliquid` (user state, mids, meta, `wait_for_deposit`, `wait_for_withdrawal`)
- Writes: `mcp__wayfinder__hyperliquid_execute`:
  - `place_order` (perp and spot, with `is_spot` flag)
  - `cancel_order`
  - `update_leverage`
  - `withdraw`
  - `spot_to_perp_transfer` / `perp_to_spot_transfer` (move USDC between wallets)

### Builder fee (“builder code”)

Builder attribution is **mandatory** in this repo:
- Builder wallet: `0xaA1D89f333857eD78F8434CC4f896A9293EFE65c`
- Fee value `f` is measured in **tenths of a basis point** (e.g. `30` → `0.030%`)
- The builder wallet is **fixed**; other addresses are rejected.

Set it in `config.json`:
- `config.json["strategy"]["builder_fee"] = {"b": "0xaA1D89f333857eD78F8434CC4f896A9293EFE65c", "f": 30}`

`mcp__wayfinder__hyperliquid_execute` will:
- attach the builder config to orders
- auto-approve the builder fee (via `approve_builder_fee`) if needed

### Spot vs perp orders (`is_spot`)

For `action="place_order"`, you **must** set `is_spot` explicitly:

**Perp order:**
```
hyperliquid_execute(
    action="place_order",
    wallet_label="main",
    coin="HYPE",
    is_spot=False,
    is_buy=True,
    usd_amount=20,
    usd_amount_kind="notional"
)
```

**Spot order:**
```
hyperliquid_execute(
    action="place_order",
    wallet_label="main",
    coin="HYPE",
    is_spot=True,
    is_buy=True,
    usd_amount=11
)
```

Note: Spot orders don't require `usd_amount_kind` (no leverage concept).

### USD sizing (avoid ambiguity)

For perp `action="place_order"`:
- Use `size` for **coin units** (e.g. ETH, HYPE).
- Or use `usd_amount` + `usd_amount_kind`:
  - `usd_amount_kind="notional"` means "position size in USD"
  - `usd_amount_kind="margin"` means "collateral in USD" (requires `leverage`; notional = margin * leverage)

For spot `action="place_order"`:
- Use `size` for **coin units**, or
- Use `usd_amount` directly (always treated as notional)

## Claude Code "execution mode" (one-off scripts)

If the user wants **immediate execution** (not a reusable strategy), prefer using the MCP tools:
- `mcp__wayfinder__hyperliquid_execute` for orders, leverage, and withdrawals
- `mcp__wayfinder__execute` for on-chain transfers (send/swap/deposit)

### `mcp__wayfinder__execute` examples

**Send tokens to another address:**
```
mcp__wayfinder__execute(
    kind="send",
    wallet_label="main",
    amount="25",
    token="usd-coin-arbitrum",
    recipient="0x112dB0cDc2A111B814138A9b3f93379f49E449F0"
)
```

**Swap tokens:**
```
mcp__wayfinder__execute(
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
mcp__wayfinder__execute(
    kind="hyperliquid_deposit",
    wallet_label="main",
    amount="8"
)
```
This hard-codes Arbitrum USDC → `HYPERLIQUID_BRIDGE_ADDRESS`. Follow with `wait_for_deposit(...)` then place the perp order.
