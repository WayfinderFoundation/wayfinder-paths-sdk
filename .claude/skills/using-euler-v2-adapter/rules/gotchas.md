# Euler v2 gotchas

## Chain support is explicit

`EulerV2Adapter` only supports chains listed in `wayfinder_paths/core/constants/euler_v2_contracts.py` (`EULER_V2_BY_CHAIN`). If you pass an unsupported `chain_id`, the adapter raises an error.

The registry is refreshed from Euler's official `EulerChains.json` and includes
current EVC/EVK/EulerEarn/EulerSwap/lens/periphery addresses. Use
`get_protocol_contracts(chain_id=...)` instead of hardcoding these addresses in
scripts.

## Vault addresses are the market (and the share token)

Euler v2 markets are **vaults**:
- The **vault address** is the market identifier you pass to adapter methods.
- The same address is also the ERC-4626 **share token** contract.

## Perspectives control which vaults you see

`get_verified_vaults(...)` and `get_all_markets(...)` read from a **Perspective** contract:
- Default is `perspective="governed"` (recommended for most strategy discovery)
- Other perspectives (e.g., `evk_factory`, `ungoverned_*`) can include riskier/unreviewed vaults

Euler docs now mark governed Perspective discovery as deprecated for verified
metadata. Use `get_labelled_vaults(...)` for current curated EVK/Earn vault
addresses, and use Perspectives only when you specifically need an on-chain
compatibility filter.

## V3 API values use different units from lens reads

Euler V3 API preview methods return:
- APYs as percent values (`5.25` means 5.25%)
- Raw on-chain amounts as strings
- USD values as numbers

The adapter mirrors normalized fields for strategy code:
- `supply_apy_decimal`, `borrow_apy_decimal`, `apy_30d_decimal`, etc. are
  decimal fractions converted from the API percent fields.
- `total_assets_raw`, `total_borrows_raw`, and related `*_raw` fields are exact
  Python int mirrors of bigint-string API fields.
- `raw` keeps the original API envelope for debugging and schema drift checks.

Lens-backed `get_all_markets(...)` returns supply/borrow APYs as decimal
fractions (`0.0525` means 5.25%) and caps/totals as ints.

## V3 API is indexed data, not transaction construction

`get_indexed_vaults(...)`, `get_indexed_vault(...)`,
`get_indexed_vault_collaterals(...)`, `get_indexed_vault_totals(...)`,
`get_euler_earn_vaults(...)`, `get_euler_earn_vault(...)`,
`resolve_vault(...)`, and `get_offchain_prices(...)` use Euler's indexed HTTP
API. Use these for discovery, monitoring, and analytics. Use lens/contract reads
when execution needs current on-chain state.

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

## Sub-accounts do not hold ERC20 tokens

EVC sub-accounts are virtual accounts for EVK/EVC accounting. Do not transfer
regular ERC20 tokens to sub-account addresses; most ERC20 contracts are not
EVC-aware and those tokens can be lost. The current write helpers default to the
strategy wallet main account and do not expose a general sub-account workflow.

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

For all indexed vaults, use `get_indexed_vaults(...)`. For current curated
vaults, use `get_labelled_vaults(...)`.

## EulerSwap is discovery-only here

The contract map includes current EulerSwap/Swapper/SwapVerifier addresses, but
the adapter does not construct Order Flow Router quotes or swap verification
calldata. Do not use these addresses to hand-roll swap or multiply batches.

## EulerEarn execution is not claimed here

EulerEarn vaults are ERC-4626 vaults, but this adapter only exposes Earn
indexed/label discovery. Do not call EVK `lend`/`unlend` methods with Earn vault
addresses; they are meant for EVK/eVault lending markets.

## MCP `get_adapter(..., wallet_label=...)` doesn’t auto-wire this adapter

`EulerV2Adapter` uses a non-standard signing callback arg (`strategy_wallet_signing_callback`), so:
- **Don’t** call `get_adapter(EulerV2Adapter, "main")` (it will error)
- **Do** wire it via `get_wallet_signing_callback(...)` and `config_overrides` as shown in `rules/execution-opportunities.md`

## Approvals are large by default

`lend(...)` and `repay(...)` call `ensure_allowance(...)` with `approval_amount=MAX_UINT256` (a very large approval). Expect a separate approval transaction the first time you interact with a given underlying/vault pair.
