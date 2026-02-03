# BRAP gotchas (avoid the common failures)

## Transaction receipts (broadcast ≠ success)

- A tx hash / “broadcasted” log does **not** mean a swap succeeded.
- The SDK waits for the receipt and raises `TransactionRevertedError` when `status=0` (often includes `gasUsed`/`gasLimit` and may indicate out-of-gas).
- In Claude Code, `mcp__wayfinder__execute(kind="swap", ...)` will surface this as `status="failed"` with an `error` string; stop and fix before continuing a multi-step flow.

## Units

- BRAP quote input `amount1` is **raw base units**.
  - If you have human units (like `"1000"` USDC), resolve decimals via TokenClient first and convert.

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
