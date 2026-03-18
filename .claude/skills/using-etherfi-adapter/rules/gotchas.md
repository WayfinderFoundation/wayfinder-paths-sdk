# ether.fi gotchas

- **Mainnet-only core flow:** this adapter only supports `chain_id=1` (Ethereum). L2 weETH token addresses exist in constants, but L2 minting/sync-pool flows are **not** implemented here.
- **No APY/rewards:** the adapter reads balances/conversions and executes stake/withdraw flows; it does **not** compute APY, rewards, or points.
- **Share-based rounding:** staking and wrap/unwrap are share-based; full-balance wraps can leave tiny dust (e.g., 1 wei).
- **Paused pool checks:** `stake_eth(..., check_paused=True)` will fail fast if the LiquidityPool is paused.
- **Approvals vs permits:** `wrap_eeth` and `request_withdraw` perform ERC20 approvals (default `MAX_UINT256`). `*_with_permit` variants skip approval but require a valid EIP-2612 permit input.
- **Withdraw request IDs:** `request_withdraw(..., include_request_id=True)` attempts to parse the minted WithdrawRequest NFT `tokenId` from the tx receipt; it can return `request_id=None` even if the tx succeeded.
- **Recipient vs owner:** `request_withdraw` supports a `recipient` (who receives the NFT). `request_withdraw_with_permit` uses `owner` (and mints the NFT to that owner).
- **Withdrawals are slow:** async withdrawals can take **days** to finalize (ether.fi processes them in batches). After `request_withdraw`, poll `is_withdraw_finalized(token_id=...)` before attempting to claim — don't assume it's ready quickly.
- **Claim requires finalization:** `claim_withdraw(token_id=...)` does not pre-check `isFinalized`; claiming early will fail (use `is_withdraw_finalized` / `get_claimable_withdraw` first).
