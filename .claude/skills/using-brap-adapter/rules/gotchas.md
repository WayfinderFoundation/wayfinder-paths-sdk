# BRAP gotchas (avoid the common failures)

## Transaction receipts (broadcast ≠ success)

- A tx hash / “broadcasted” log does **not** mean a swap succeeded.
- The SDK waits for the receipt and raises `TransactionRevertedError` when `status=0` (often includes `gasUsed`/`gasLimit` and may indicate out-of-gas).
- In Claude Code, `mcp__wayfinder__onchain_swap` will surface this as `status="failed"` with an `error` string; stop and fix before continuing a multi-step flow.

## Units

- BRAP quote input `amount1` is **raw base units**.
  - If you have human units (like `"1000.0"` USDC), resolve decimals via TokenClient first and convert.
- The MCP helper tools (`onchain_quote_swap` / `onchain_swap`) are different: they take decimal human-unit strings and convert to raw units internally. Include a decimal point, for example `"1000.0"` instead of `"1000"`.

## Slippage formats

- BRAP client/adapters use slippage as a **decimal fraction** (`0.005` = 0.5%).
- MCP helper tooling may use bps (`50` = 0.5%). Don’t mix these.

## USD enrichment is best-effort

- The quote backend may log errors while enriching USD values (e.g. type issues) even when the quote succeeds.
- Treat USD fields as optional; rely on raw amounts + calldata for correctness.

## Approvals

- ERC20 approvals may be required before swap execution.
- Some tokens are “strict approve” and require setting allowance to `0` before increasing it; the adapter has a built-in allowlist for this.

## Recipient safety

- Treat `recipient != sender` as a high-risk condition. Require explicit user confirmation and display the mismatch clearly.

## Native token sends (execute tool)

- For tiny amounts (e.g., 1 wei), use **scientific notation**: `"1e-18"` works, but `"0.000000000000000001"` may cause serialization errors.
- Native sends require `token: "native"` and `chain_id` in the request.

## Low-cap / meme tokens on new chains

Newer chains (e.g. Robinhood) are mostly micro-cap memes the standard catalog hasn't indexed. Before quoting or swapping one:

- **Browse with `onchain_list_tokens(chain_code, dimension)`** (`trending` | `volume` | `new` | `active`) to see what's actually live — including brand-new launches — with price, liquidity, 24h volume, FDV, pool age, and DEX.
- **Resolve a pasted address first** (`onchain_resolve_token` / `onchain_fuzzy_search_tokens`); never infer a token's identity or purpose from its name/symbol.
- **Size for the liquidity.** Micro-caps (FDV < ~$1M, liquidity < ~$50k, days old, unverified) have thin pools: a large order gets a poor price or no quote. Quote a small clip first, surface the risk read (liquidity, FDV, age, fillable size), and confirm before executing.
