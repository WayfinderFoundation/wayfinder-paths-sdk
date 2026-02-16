# Hyperlend Adapter

Adapter for the HyperLend lending protocol on HyperEVM (chain ID `999`).

- **Type**: `HYPERLEND`
- **Module**: `wayfinder_paths.adapters.hyperlend_adapter.adapter.HyperlendAdapter`

## Overview

The HyperlendAdapter provides:
- Stable-market discovery via backend API (headroom-filtered)
- Full on-chain reserve listing (no filtering) via `UiPoolDataProvider`
- User asset views via backend API
- Lending + borrowing (supply/withdraw/borrow/repay) via the Pool contract
- Collateral toggles via `Pool.setUserUseReserveAsCollateral(...)`

## Protocol Addresses (HyperEVM)

- **Pool**: `0x00A89d7a5A02160f20150EbEA7a2b5E4879A1A8b`
- **Pool Addresses Provider**: `0x72c98246a98bFe64022a3190e7710E157497170C`
- **UiPoolDataProvider**: `0x3Bb92CF81E38484183cc96a4Fb8fBd2d73535807`
- **Wrapped Token Gateway**: `0x49558c794ea2aC8974C9F27886DDfAa951E99171`

## Usage

```python
from wayfinder_paths.adapters.hyperlend_adapter import HyperlendAdapter

adapter = HyperlendAdapter(config={})
```

## Methods

### get_all_markets (on-chain)

List all reserves directly from `UiPoolDataProvider.getReservesData(...)` (no backend filtering).

```python
success, markets = await adapter.get_all_markets()
```

Each market entry includes:
- `underlying`, `symbol`, `symbol_canonical`, `decimals`
- `a_token`, `variable_debt_token`
- Flags: `is_active`, `is_frozen`, `is_paused`, `is_siloed_borrowing`
- Flags: `usage_as_collateral_enabled`, `borrowing_enabled`
- `is_stablecoin`
- Rates: `supply_apr`, `supply_apy`, `variable_borrow_apr`, `variable_borrow_apy`
- Liquidity: `available_liquidity`, `total_variable_debt`, `tvl`
- Caps: `supply_cap`, `supply_cap_headroom`

### get_stable_markets (backend)

Fetch stablecoin markets that meet headroom requirements (pre-filtered by the backend).

```python
success, data = await adapter.get_stable_markets(
    required_underlying_tokens=1000.0,
    buffer_bps=50,
    min_buffer_tokens=0.5,
)
```

### get_assets_view (backend)

Fetch a user’s HyperLend asset snapshot (supplies/borrows, prices, rates).

```python
success, view = await adapter.get_assets_view(user_address="0x...")
```

### get_full_user_state (backend, positions snapshot)

Convenience wrapper over `get_assets_view(...)` that returns a standardized snapshot:

```python
success, state = await adapter.get_full_user_state(
    account="0x...",
    include_zero_positions=False,
)
```

Returns a dict with:
- `protocol`, `account`
- `positions` (filtered assets view entries by default)
- `accountData` (summary health/account fields from the backend)
- `assetsView` (raw backend response)

### lend / unlend (on-chain)

Supply/withdraw via the Pool contract.

```python
success, tx_hash = await adapter.lend(
    underlying_token="0x...",
    qty=123,
    chain_id=999,
)

success, tx_hash = await adapter.unlend(
    underlying_token="0x...",
    qty=123,
    chain_id=999,
)
```

### borrow / repay (on-chain)

Borrow and repay via the Pool contract (Aave-style). Uses variable rate mode (`2`).

```python
success, tx_hash = await adapter.borrow(
    underlying_token="0x...",
    qty=123,
    chain_id=999,
)

success, tx_hash = await adapter.repay(
    underlying_token="0x...",
    qty=123,
    chain_id=999,
    repay_full=False,
)
```

For native HYPE:
- `borrow(native=True)` borrows WHYPE via the Pool, then unwraps to HYPE (two txs).
- `repay(native=True)` uses the Wrapped Token Gateway. For a native full repay, use
  `repay_full=True` (the adapter reads current debt on-chain and sends a buffered `msg.value`).

### set_collateral / remove_collateral (on-chain)

Enable/disable an underlying asset as collateral.

```python
success, tx_hash = await adapter.set_collateral(underlying_token="0x...", chain_id=999)
success, tx_hash = await adapter.remove_collateral(underlying_token="0x...", chain_id=999)
```

## Return Format

All methods return `(success: bool, data: Any)` tuples.

## Testing

```bash
poetry run pytest wayfinder_paths/adapters/hyperlend_adapter/ -v
```
