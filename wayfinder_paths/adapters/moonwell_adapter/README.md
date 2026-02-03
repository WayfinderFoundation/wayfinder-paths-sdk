# Moonwell Adapter

Adapter for the [Moonwell](https://moonwell.fi/) lending protocol on Base.

- **Type**: `MOONWELL`
- **Module**: `wayfinder_paths.adapters.moonwell_adapter.adapter.MoonwellAdapter`

## Overview

The MoonwellAdapter provides:
- Lending (supply/withdraw)
- Borrowing (borrow/repay)
- Collateral management
- WELL rewards claiming
- Position and market queries

## Supported Markets (Base)

| Token | mToken Address | Underlying Address |
|-------|----------------|-------------------|
| USDC | `0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22` | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` |
| WETH | `0x628ff693426583D9a7FB391E54366292F509D457` | `0x4200000000000000000000000000000000000006` |
| wstETH | `0x627Fe393Bc6EdDA28e99AE648fD6fF362514304b` | `0xc1CBa3fCea344f92D9239c08C0568f6F2F0ee452` |

## Protocol Addresses (Base)

- **Comptroller**: `0xfbb21d0380bee3312b33c4353c8936a0f13ef26c`
- **Reward Distributor**: `0xe9005b078701e2a0948d2eac43010d35870ad9d2`
- **WELL Token**: `0xA88594D404727625A9437C3f886C7643872296AE`

## Usage

```python
from wayfinder_paths.adapters.moonwell_adapter import MoonwellAdapter

config = {
    "strategy_wallet": {"address": "0x..."},
    "moonwell_adapter": {
        "chain_id": 8453,
    }
}
adapter = MoonwellAdapter(config=config)
```

## Methods

### Lending

```python
# Supply tokens
await adapter.lend(
    mtoken="0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22",
    underlying_token="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    amount=1000 * 10**6,
)

# Withdraw tokens
await adapter.unlend(
    mtoken="0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22",
    amount=500 * 10**6,
)
```

### Borrowing

```python
# Borrow
await adapter.borrow(
    mtoken="0x628ff693426583D9a7FB391E54366292F509D457",
    amount=10**17,  # 0.1 WETH
)

# Repay
await adapter.repay(
    mtoken="0x628ff693426583D9a7FB391E54366292F509D457",
    underlying_token="0x4200000000000000000000000000000000000006",
    amount=10**17,
)
```

### Collateral Management

```python
# Enable as collateral
await adapter.set_collateral(
    mtoken="0x627Fe393Bc6EdDA28e99AE648fD6fF362514304b",
)

# Disable collateral
await adapter.remove_collateral(
    mtoken="0x627Fe393Bc6EdDA28e99AE648fD6fF362514304b",
)
```

### Position Queries

```python
# Get position data
success, position = await adapter.get_pos(
    mtoken="0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22",
)

# Get collateral factor
success, cf = await adapter.get_collateral_factor(
    mtoken="0x627Fe393Bc6EdDA28e99AE648fD6fF362514304b",
)

# Get APY
success, apy = await adapter.get_apy(
    mtoken="0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22",
    apy_type="supply",  # or "borrow"
)

# Get max borrowable
success, amount = await adapter.get_borrowable_amount(account="0x...")
```

### Rewards

```python
# Claim WELL rewards
await adapter.claim_rewards(min_rewards_usd=1.0)
```

## Return Format

All methods return `(success: bool, data: Any)` tuples.

## Testing

```bash
poetry run pytest wayfinder_paths/adapters/moonwell_adapter/ -v
```
