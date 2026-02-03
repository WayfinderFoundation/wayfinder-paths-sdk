# Pools (opportunity universe)

## Primary data source

- Client: `wayfinder_paths/core/clients/PoolClient.py`
- Adapter wrapper: `wayfinder_paths/adapters/pool_adapter/adapter.py`

## High-value reads

### List pools (discovery)

- Call: `PoolClient.get_pools(chain_id: int | None = None, project: str | None = None)`
- Wrapper: `PoolAdapter.get_pools(chain_id=..., project=...)`
- Returns: `{"matches": [PoolData...]}` (schema-flexible; treat keys as optional)

Common fields in each pool entry include:
- `id` (also mirrored as `pool_id` / `token_id`)
- `project`, `chain` / `network` / `chain_code`
- `symbol`, `address`
- optional yield/liquidity fields (e.g., `apy`, `apyBase`, `apyReward`, `tvlUsd`)

### Get pools by IDs (shortlist)

- Call: `PoolClient.get_pools_by_ids(pool_ids=[...])`
- Wrapper: `PoolAdapter.get_pools_by_ids(pool_ids=[...])`
- Returns: `{"pools": [PoolData...]}` (normalized entries)

## Strategy patterns

- “Screen then validate”:
  1) pull a broad list (`get_pools`)
  2) filter by chain + stablecoin-only + TVL floor
  3) only then request deeper analytics for the finalists
