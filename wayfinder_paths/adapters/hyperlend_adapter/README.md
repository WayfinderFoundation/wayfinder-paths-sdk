# Hyperlend Adapter

Adapter for the HyperLend lending protocol on HyperEVM (chain ID `999`).

- **Type**: `HYPERLEND`
- **Module**: `wayfinder_paths.adapters.hyperlend_adapter.adapter.HyperlendAdapter`

## Overview

The HyperlendAdapter provides:
- Stable-market discovery via backend API (headroom-filtered)
- Full on-chain reserve listing (no filtering) via `UiPoolDataProvider`
- User asset views via backend API
- Lending (supply/withdraw) via the Pool contract

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

## Return Format

All methods return `(success: bool, data: Any)` tuples.

## Testing

```bash
poetry run pytest wayfinder_paths/adapters/hyperlend_adapter/ -v
```

