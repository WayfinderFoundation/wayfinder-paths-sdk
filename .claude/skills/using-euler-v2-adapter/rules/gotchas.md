# Euler v2 gotchas

## Chain support is explicit

`EulerV2Adapter` only supports chains listed in `wayfinder_paths/core/constants/euler_v2_contracts.py` (`EULER_V2_BY_CHAIN`). If you pass an unsupported `chain_id`, the adapter raises an error.

## Vault addresses are the market (and the share token)

Euler v2 markets are **vaults**:
- The **vault address** is the market identifier you pass to adapter methods.
- The same address is also the ERC-4626 **share token** contract.

## Perspectives control which vaults you see

`get_verified_vaults(...)` and `get_all_markets(...)` read from a **Perspective** contract:
- Default is `perspective="governed"` (recommended for most strategy discovery)
- Other perspectives (e.g., `evk_factory`, `ungoverned_*`) can include riskier/unreviewed vaults

## Units are raw ints

All `amount` parameters are **raw integer units** of the **underlying** token (unless noted):
- `lend(..., amount=...)` deposits underlying units
- `borrow(..., amount=...)` borrows underlying units
- `repay(..., amount=...)` repays underlying units
- `unlend(..., amount=...)` withdraws underlying units

For full exits, prefer:
- `repay(..., repay_full=True)` then
- `unlend(..., withdraw_full=True)` (redeems **all shares** based on `vault.balanceOf(strategy)`).

## Collateral is not automatic

Depositing into a vault does **not** automatically enable it as collateral. You must:
- call `set_collateral(..., use_as_collateral=True)`, or
- pass the vault in `borrow(..., collateral_vaults=[...])` to enable in the same EVC batch.

## Controller must be enabled for borrows

Borrowing from a vault generally requires enabling that vault as the **controller** for your account:
- `borrow(..., enable_controller=True)` (default) batches `enableController` before `borrow`
- If you set `enable_controller=False`, borrowing may revert unless a controller is already enabled

## `repay_full=True` uses MAX_UINT256

`repay(..., repay_full=True)` uses `MAX_UINT256` repayment semantics:
- You still need enough underlying balance to cover the full debt at execution time
- The adapter sets a large allowance (up to `MAX_UINT256`) on the underlying token for the vault

## `get_all_markets` can return partial results

If some vault lens calls fail, the adapter logs warnings and returns:
- `ok=True` with the markets that succeeded, as long as at least one vault fetched successfully
- `ok=False` only if **all** vault fetches fail

## `get_all_markets` is perspective-scoped

`get_all_markets(...)` fetches the current `verifiedArray()` for the selected **Perspective**. It does not attempt to discover “all vaults on-chain”.

## MCP `get_adapter(..., wallet_label=...)` doesn’t auto-wire this adapter

`EulerV2Adapter` uses a non-standard signing callback arg (`strategy_wallet_signing_callback`), so:
- **Don’t** call `get_adapter(EulerV2Adapter, "main")` (it will error)
- **Do** wire it via `get_wallet_signing_callback(...)` and `config_overrides` as shown in `rules/execution-opportunities.md`

## Approvals are large by default

`lend(...)` and `repay(...)` call `ensure_allowance(...)` with `approval_amount=MAX_UINT256` (a very large approval). Expect a separate approval transaction the first time you interact with a given underlying/vault pair.
