# Polymarket collateral: routing in and out of pUSD

This page covers **collateral routing** — moving between any token/chain and pUSD on the **owner EOA**. For funding the per-user **deposit wallet** (the actual trading address under V2), see `rules/deposit-wallet.md`. A full trade lifecycle uses both flows.

## Key requirement

- Polymarket V2 CLOB trading collateral is **pUSD** on Polygon:
  - `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` (proxy, 6 decimals)
- Polygon **USDC.e** wraps 1:1 to pUSD:
  - `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` (6 decimals)
- Native Polygon **USDC**:
  - `0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359`

## MCP recipe — BRAP swap tools

Route into and out of pUSD with the BRAP swap MCP tools. They pick the right solver automatically (`polymarket_bridge` for USDC.e ↔ pUSD 1:1 wraps; standard DEX routes for everything else; cross-chain bridges when source chain ≠ Polygon).

**In: any token → pUSD on Polygon**

```
mcp__wayfinder__onchain_quote_swap(
    wallet_label="main",
    from_token="<source token id>",          # e.g. "polygon_0x3c499c..." (Polygon USDC)
    to_token="polygon_0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB",  # pUSD
    amount="<wei>",                          # use to_erc20_raw(human, decimals)
    slippage_bps=50,
)
# inspect the preview, then:
mcp__wayfinder__core_execute(request=<suggested_execute_request>)
```

**Out: pUSD → any token**

Flip `from_token` and `to_token`. For pUSD → native Polygon USDC, BRAP routes via the polymarket_bridge unwrap + USDC.e/USDC swap. For pUSD → another chain, BRAP picks a cross-chain route.

**Important**: `from_token` / `to_token` accept `<chain_code>_<address>` ids, `<coingecko_id>-<chain_code>` ids, or symbol queries. `amount` is raw wei (use `to_erc20_raw(human, decimals)` to convert). See `onchain_quote_swap` for full arg docs.

## Already have pUSD?

Skip routing entirely and call `polymarket_fund_deposit_wallet(...)` to move pUSD from the owner EOA into the deposit wallet (the actual trading address — see `rules/deposit-wallet.md`).
