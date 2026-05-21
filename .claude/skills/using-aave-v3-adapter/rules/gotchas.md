# Aave V3 gotchas

- **Chain matters:** always pass the correct `chain_id`. Aave V3 deployments are per-chain.
- **Variable rate mode:** borrowing/repaying uses variable rate mode (`interestRateMode=2`).
- **Collateral toggle:** supplying an asset doesn't always mean it's enabled as collateral; use `set_collateral(...)`.
- **Risk modes:** check `emode_category_id`, `borrowable_in_isolation`, `is_siloed_borrowing`, `debt_ceiling`, and `account_data.health_factor` before suggesting a borrow, collateral change, or eMode change.
- **eMode:** `set_emode(...)` only accepts IDs `0..255`; `0` disables eMode. Aave can still revert if the wallet has incompatible collateral or debt.
- **Rewards inputs:** rewards are incentivized on **aTokens and debt tokens**, not the underlying. `claim_all_rewards(...)` can auto-derive the asset list.
- **Aave Earn vaults:** Earn vaults are ERC-4626 vaults over Aave V3 supply positions. Use `get_earn_vault_state(...)` first, and use the `*_atokens` methods only when the wallet holds the reserve aToken.
- **UiPoolDataProvider variants:** live Aave V3 deployments do not all expose the same reserve tuple shape. The adapter handles current compact, Origin, and legacy periphery shapes; do not hand-roll tuple indexes in scripts.
- **V4 scope:** Aave V4 uses a different hub/spoke model. Do not mix V4 behavior into this V3 adapter; document it as follow-up work instead.
- **Native token handling:** `native=True` wraps/unwraps the chain's wrapped native token and may return multiple tx hashes for a single call.
- **repay_full:** `repay_full=True` uses `MAX_UINT256` repayment semantics; ensure you have enough balance (and native gas if wrapping).
