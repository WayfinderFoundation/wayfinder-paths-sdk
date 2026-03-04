# Ethena sUSDe gotchas

- **Mainnet-only vault:** execution methods (`deposit_usde`, `request_withdraw_*`, `claim_withdraw`) target the canonical sUSDe vault on Ethereum mainnet (no `chain_id` parameter).
- **Two-step withdrawals:** there is no single-call withdraw. You must call `cooldownShares`/`cooldownAssets` first, then wait, then call `unstake` via `claim_withdraw(...)`.
- **Units are raw ints:** `amount_assets`, `assets`, and `shares` are raw token units (USDe/sUSDe are 18 decimals on mainnet).
- **`claim_withdraw` may not return a tx hash:** if `underlyingAmount == 0`, it returns `(True, "no pending cooldown")`.
- **Cross-chain reads:** `get_full_user_state(chain_id != 1)` reads USDe/sUSDe balances on the target chain, but reads cooldown + `convertToAssets` on mainnet (needs RPC access to both).
- **`include_apy` adds extra calls:** only set `include_apy=True` when you actually need the spot APY.
- **Always check `(success, data)`**: errors are returned as strings.
