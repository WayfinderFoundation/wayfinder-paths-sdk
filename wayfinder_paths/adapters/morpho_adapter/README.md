# Morpho Adapter (Morpho Blue / Markets)

Adapter for Morpho Blue (Markets). Unlike pool-style lending protocols, Morpho actions are **market-specific**: every market is identified by a `market_unique_key` (bytes32 hex string) and has immutable parameters `(loanToken, collateralToken, oracle, irm, lltv)`.

- **Type**: `MORPHO`
- **Module**: `wayfinder_paths.adapters.morpho_adapter.adapter.MorphoAdapter`

## Usage

```python
from wayfinder_paths.adapters.morpho_adapter import MorphoAdapter

adapter = MorphoAdapter(config={})
```

## Methods

### get_all_markets (off-chain via Morpho API)

```python
success, markets = await adapter.get_all_markets(chain_id=8453)
```

Returns market snapshots including `uniqueKey`, loan/collateral assets, `lltv`, oracle/IRM addresses, and point-in-time `supply_apy` / `borrow_apy` from Morpho’s API.

### get_market_entry / get_pos

```python
success, market = await adapter.get_market_entry(chain_id=8453, market_unique_key="0x...")
success, pos = await adapter.get_pos(chain_id=8453, market_unique_key="0x...", account="0x...")
```

### Supply / Withdraw (lend / unlend)

```python
success, tx_hash = await adapter.lend(chain_id=8453, market_unique_key="0x...", qty=123)
success, tx_hash = await adapter.unlend(chain_id=8453, market_unique_key="0x...", qty=123)
```

### Collateral (deposit / withdraw)

```python
success, tx_hash = await adapter.supply_collateral(chain_id=8453, market_unique_key="0x...", qty=123)
success, tx_hash = await adapter.withdraw_collateral(chain_id=8453, market_unique_key="0x...", qty=123)
```

### Borrow / Repay

```python
success, tx_hash = await adapter.borrow(chain_id=8453, market_unique_key="0x...", qty=123)
success, tx_hash = await adapter.repay(chain_id=8453, market_unique_key="0x...", qty=123)
success, tx_hash = await adapter.repay(chain_id=8453, market_unique_key="0x...", qty=0, repay_full=True)
```

Notes:
- `repay_full=True` repays by shares (read on-chain via `Morpho.position(...)`) to avoid dust from interest accrual.

## Return Format

All methods return `(success: bool, data: Any)` tuples.

## Testing

```bash
poetry run pytest wayfinder_paths/adapters/morpho_adapter/ -v
```

