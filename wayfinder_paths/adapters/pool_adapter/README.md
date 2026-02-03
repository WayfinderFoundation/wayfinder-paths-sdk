# Pool Adapter

Adapter for DeFi pool data and yield analytics.

- **Type**: `POOL`
- **Module**: `wayfinder_paths.adapters.pool_adapter.adapter.PoolAdapter`

## Overview

The PoolAdapter provides:
- Pool information and metadata
- Yield analytics via DeFi Llama integration
- Pool discovery and filtering

## Usage

```python
from wayfinder_paths.adapters.pool_adapter.adapter import PoolAdapter

adapter = PoolAdapter()
```

## Methods

### get_pools_by_ids

Fetch pool information by pool IDs.

```python
success, data = await adapter.get_pools_by_ids(
    pool_ids=["pool-123", "pool-456"]
)
if success:
    pools = data.get("pools", [])
```

### get_pools

Fetch pools with optional filtering.

```python
success, data = await adapter.get_pools(
    chain_id=8453,      # Filter by chain (e.g., Base)
    project="lido",     # Optional: filter by project
)
if success:
    matches = data.get("matches", [])
```

## Response Format

Pool data includes:
- `id` - Pool identifier
- `apy` - Current APY
- `tvl` - Total value locked
- `chain` - Chain information
- `project` - Protocol name
- `stablecoin` - Whether pool is stablecoin-based

## Dependencies

- `PoolClient` - Low-level API client

## Testing

```bash
poetry run pytest wayfinder_paths/adapters/pool_adapter/ -v
```
