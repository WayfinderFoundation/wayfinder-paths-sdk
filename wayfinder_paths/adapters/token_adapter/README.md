# Token Adapter

Adapter for token metadata and price feeds.

- **Type**: `TOKEN`
- **Module**: `wayfinder_paths.adapters.token_adapter.adapter.TokenAdapter`

## Overview

The TokenAdapter provides:
- Token metadata (address, decimals, symbol)
- Live price data
- Gas token lookups by chain

## Usage

```python
from wayfinder_paths.adapters.token_adapter.adapter import TokenAdapter

adapter = TokenAdapter()
```

## Methods

### get_token

Get token metadata by address or token ID.

```python
# By token ID
success, data = await adapter.get_token("usd-coin-base")

# By address
success, data = await adapter.get_token("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")

if success:
    print(f"Symbol: {data['symbol']}")
    print(f"Decimals: {data['decimals']}")
    print(f"Address: {data['address']}")
```

### get_token_price

Get current price data for a token.

```python
success, data = await adapter.get_token_price("usd-coin-base")
if success:
    print(f"Price: ${data['current_price']}")
    print(f"24h Change: {data['price_change_percentage_24h']}%")
```

### get_gas_token

Get the native gas token for a chain.

```python
success, data = await adapter.get_gas_token("base")
if success:
    print(f"Gas token: {data['symbol']}")
```

## Response Format

Token metadata includes:
- `symbol` - Token symbol (e.g., "USDC")
- `name` - Full name
- `address` - Contract address
- `decimals` - Token decimals
- `chain_id` - Chain ID
- `token_id` - Wayfinder token identifier

## Dependencies

- `TokenClient` - Low-level API client

## Testing

```bash
poetry run pytest wayfinder_paths/adapters/token_adapter/ -v
```
