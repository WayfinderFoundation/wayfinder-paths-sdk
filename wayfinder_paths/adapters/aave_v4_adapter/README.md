# Aave v4 (Arc mainnet)

Separate from Aave v3: Arc chain 5042 uses a Hub and `main` / `forex` Spokes,
not the v3 Pool ABI. Addresses come from `AaveV4Arc` in the Aave address book.
Read methods return `(ok, data)`; writes return `(ok, tx_hash)`.

```python
from wayfinder_paths.adapters.aave_v4_adapter.adapter import AaveV4Adapter

adapter = AaveV4Adapter()
ok, reserves = await adapter.get_markets(spoke="main")
ok, forex = await adapter.get_markets(spoke="forex")
```

For writes obtain an adapter through the existing `get_adapter` wallet/signing
flow. Never infer a reserve ID from another Spoke: match the underlying address
in `get_markets` and pass that row's `reserve_id` and `spoke` together.

- `get_reserve`, `get_markets`: underlying, decimals, pause/freeze/borrowability,
  Hub liquidity and spoke-specific caps (caps are whole-token units).
- `get_user_state(account=..., spoke=...)`: supplied assets, debt and account
  health. Account data uses protocol raw units, not display percentages.
- `supply`, `withdraw`, `borrow`, `repay(reserve_id, amount, spoke=...)`: raw
  **ERC-20** units. USDC uses **6 decimals**, not Arc native gas's 18. Borrowing
  requires collateral; pending-state simulation enforces protocol restrictions.
- `set_collateral(reserve_id, enabled, spoke=...)`: enable/disable that reserve.
- `tokenized_deposit(symbol, assets)` / `tokenized_redeem(symbol, shares)`:
  USDC, EURC, cirBTC and WETH ERC-4626 spokes. Reuses Morpho's vault execution
  helpers; these are supplied assets, not borrowing accounts.

Arc is not gas-sponsored. Keep native USDC for gas and do not add native and
ERC-20 USDC balances together. Supply approvals use only the requested amount.
No actions create leverage automatically, select a strategy, or sweep positions.

Mocked write-path regressions run in the normal Adapter Tests job. The Arc
integration job adds live read-only deployment, reserve/position, discovery and
quote checks on every check-in. It needs no API key and never sends transactions.
Gorlami does not support Arc, so full Arc transaction execution is not covered.
The separate existing-chain fork scenarios remain manual tests, not part of the
Arc CI job.
