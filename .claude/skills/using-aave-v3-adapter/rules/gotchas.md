# Aave V3 gotchas

- **Chain matters:** always pass the correct `chain_id`. Aave V3 deployments are per-chain.
- **Variable rate mode:** borrowing/repaying uses variable rate mode (`interestRateMode=2`).
- **Collateral toggle:** supplying an asset doesn't always mean it's enabled as collateral; use `set_collateral(...)`.
- **Rewards inputs:** rewards are incentivized on **aTokens and debt tokens**, not the underlying. `claim_all_rewards(...)` can auto-derive the asset list.
- **Native token handling:** `native=True` wraps/unwraps the chain's wrapped native token and may return multiple tx hashes for a single call.
- **repay_full:** `repay_full=True` uses `MAX_UINT256` repayment semantics; ensure you have enough balance (and native gas if wrapping).
