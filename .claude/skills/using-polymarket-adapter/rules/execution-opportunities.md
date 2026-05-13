# Polymarket execution (buy/sell + orders + redeem)

## MCP tools (Claude Code)

- Read-only: `mcp__wayfinder__polymarket_read` (search, market metadata, prices/books/history) and `mcp__wayfinder__polymarket_get_state` (account state)
- Writes: `mcp__wayfinder__polymarket_execute` (prepare / unwind collateral, buy/sell, limit/cancel, close, redeem)

## Preconditions (for write paths)

- Polygon RPC configured (`strategy.rpc_urls["137"]`)
- Wallet configured (local with `private_key_hex` or remote via Privy)
- Have Polygon gas token (POL) on the **owner EOA** — funding the deposit wallet costs gas
- Have **pUSD** ready on the owner EOA (see `rules/deposits-withdrawals.md` to prepare it from USDC/USDC.e)
- **Deposit wallet funded** — orders execute from the per-user deposit wallet, not the owner EOA. Use `polymarket_execute(action="fund_deposit_wallet", amount=...)` before trading. See `rules/deposit-wallet.md`.

## Deposit wallet setup + API creds (automatic, cached)

`ensure_trading_setup()` is idempotent and called before every order. On the first call it:

- deploys the per-user deposit wallet (if missing) via the relayer
- batches pUSD ERC20 allowance + ConditionalTokens ERC1155 `setApprovalForAll` for the three exchange addresses into one relayer-signed call
- derives CLOB API creds (`ensure_api_creds()`)
- syncs CLOB balance allowance for COLLATERAL (+ CONDITIONAL if a `token_id` is passed)

After the first successful call, `_setup_complete=True` and subsequent orders short-circuit. **It does not fund the deposit wallet** — that's an explicit step (`fund_deposit_wallet`).

## Buying (place prediction)

Fast path:

```python
ok, res = await adapter.place_prediction(
    market_slug="bitcoin-above-70k-on-february-9",
    outcome="YES",
    amount_collateral=2.0,  # dollar-denominated buy amount; spent as pUSD collateral under V2
)
```

MCP shortcut:

- `mcp__wayfinder__polymarket_execute(action="buy", wallet_label="main", market_slug="bitcoin-above-70k-on-february-9", outcome="YES", amount_collateral=2)`

Lower-level control (CLOB token id + side):

```python
ok_tid, token_id = adapter.resolve_clob_token_id(market=market, outcome="YES")
ok, res = await adapter.place_market_order(token_id=token_id, side="BUY", amount=2.0)
```

Important: `place_market_order()` semantics:

- `side="BUY"` → `amount` is **collateral ($) to spend**
- `side="SELL"` → `amount` is **shares to sell**

## Selling (cash out)

```python
ok, res = await adapter.cash_out_prediction(
    market_slug="bitcoin-above-70k-on-february-9",
    outcome="YES",
    shares=1.0,
)
```

MCP shortcuts:

- Sell partial: `mcp__wayfinder__polymarket_execute(action="sell", wallet_label="main", market_slug="...", outcome="...", shares=1)`
- Sell full position size: `mcp__wayfinder__polymarket_execute(action="close_position", wallet_label="main", market_slug="...", outcome="...")`

Practical note (important): after a BUY, there can be a **settlement lag** before shares are sellable. If you’re chaining BUY → SELL in automation, wait for the buy match transaction to confirm (the CLOB response typically includes `transactionsHashes`).

## Orders (limit / cancel / open orders)

- `place_limit_order(token_id, side, price, size, post_only=False)`
- `cancel_order(order_id=...)`
- `list_open_orders(token_id=...)`

MCP shortcuts:

- Place limit: `mcp__wayfinder__polymarket_execute(action="place_limit_order", wallet_label="main", token_id="...", side="BUY", price=0.42, size=10)`
- Cancel order: `mcp__wayfinder__polymarket_execute(action="cancel_order", wallet_label="main", order_id="...")`
- List open orders: `mcp__wayfinder__polymarket_read(action="open_orders", wallet_label="main")`

## Redemption (resolved markets)

If you held shares through resolution, redeem on-chain:

1) Get `conditionId` from Gamma market metadata
2) Call:

```python
ok, res = await adapter.redeem_positions(
    condition_id=condition_id,
    holder="0xYourWallet",  # must match signing wallet
)
```

MCP shortcut:

- `mcp__wayfinder__polymarket_execute(action="redeem_positions", wallet_label="main", condition_id="0x...")`

The adapter preflights the redemption path and calls ConditionalTokens `redeemPositions()`. Some markets can pay out an “adapter collateral” wrapper token; the adapter attempts to unwrap automatically.
