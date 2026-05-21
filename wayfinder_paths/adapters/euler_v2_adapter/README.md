# Euler v2 (EVK / eVault) Adapter

- **Module**: `wayfinder_paths.adapters.euler_v2_adapter.adapter.EulerV2Adapter`
- **Protocol**: Euler v2 (Euler Vault Kit / eVaults)

## Notes

- Markets are **vaults** (the vault address is also the ERC-4626 share token).
- The adapter uses **EVC (Ethereum Vault Connector)** batching for vault operations.
- Contract addresses are sourced from Euler's `euler-interfaces/EulerChains.json`
  registry and include EVK, EVC, EulerEarn, EulerSwap, lenses, perspectives, and
  swap periphery addresses.
- Use `get_labelled_vaults(...)` for current curated EVK/Earn vault discovery
  from `euler-labels`. On-chain `get_verified_vaults(...)` still works for
  Perspective compatibility, but Euler docs mark governed Perspective discovery
  as deprecated for verified metadata.
- Use `get_indexed_vaults(...)`, `get_euler_earn_vaults(...)`,
  `resolve_vault(...)`, and `get_offchain_prices(...)` for Euler V3 API preview
  reads. The V3 API returns APYs as percent values and raw on-chain amounts as
  strings.
- `get_all_markets(...)` remains an on-chain lens read for EVK vault state and
  returns APYs as decimal fractions.
- EulerSwap/order-flow execution is not implemented here. The registry exposes
  Swapper and SwapVerifier addresses for discovery and safety review only.

## Supported Surface

| Surface | Adapter support | Notes |
|---------|-----------------|-------|
| EVK / eVault on-chain reads | Supported | `get_verified_vaults`, `get_vault_info_full`, `get_all_markets`, `get_full_user_state` read current contract/lens state. |
| EVK / EVC fund-moving flows | Supported | `lend`, `unlend`, `set_collateral`, `remove_collateral`, `borrow`, and `repay` execute EVC batches and require a strategy wallet signing callback. |
| Euler V3 API preview | Read-only | `get_indexed_vaults`, `get_indexed_vault`, `get_indexed_vault_collaterals`, `get_indexed_vault_totals`, `resolve_vault`, and `get_offchain_prices` use indexed HTTP data. These calls are not transaction builders. |
| EulerEarn | Read-only discovery | `get_euler_earn_vaults`, `get_euler_earn_vault`, and `get_labelled_vaults` expose indexed/label data. Earn deposit and withdraw methods are not implemented in this adapter. |
| EulerSwap / Order Flow Router | Discovery only | Contract addresses are exposed through `get_protocol_contracts`; swap payload construction and execution are intentionally out of scope until quote, route, slippage, and `SwapVerifier` policy integration exists. |

V3 API payloads are normalized at the SDK boundary:

- EVM addresses are checksummed.
- Raw bigint-style amounts remain in the original camelCase fields and are also
  mirrored as Python ints in `*_raw` fields where the API returns integer
  strings.
- V3 APY percent fields remain in the original camelCase fields and are also
  mirrored as decimal fractions in `*_decimal` fields.
- The original API envelope is preserved under `raw`.
