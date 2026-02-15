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

### get_market_entry / get_market_state / get_market_historical_apy

```python
success, market = await adapter.get_market_entry(chain_id=8453, market_unique_key="0x...")
success, market = await adapter.get_market_state(chain_id=8453, market_unique_key="0x...")
success, hist = await adapter.get_market_historical_apy(chain_id=8453, market_unique_key="0x...", interval="DAY")
success, pos = await adapter.get_pos(chain_id=8453, market_unique_key="0x...", account="0x...")
```

### Supply / Withdraw (lend / unlend)

```python
success, tx_hash = await adapter.lend(chain_id=8453, market_unique_key="0x...", qty=123)
success, tx_hash = await adapter.unlend(chain_id=8453, market_unique_key="0x...", qty=123)
success, tx_hash = await adapter.unlend(chain_id=8453, market_unique_key="0x...", qty=0, withdraw_full=True)
success, tx_hash = await adapter.withdraw_full(chain_id=8453, market_unique_key="0x...")
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
success, tx_hash = await adapter.repay_full(chain_id=8453, market_unique_key="0x...")
```

Notes:
- `repay_full=True` repays by shares (read on-chain via `Morpho.position(...)`) to avoid dust from interest accrual.

### Risk helpers (off-chain + computed)

```python
success, health = await adapter.get_health(chain_id=8453, market_unique_key="0x...")
success, max_borrow = await adapter.max_borrow(chain_id=8453, market_unique_key="0x...")
success, max_withdraw = await adapter.max_withdraw_collateral(chain_id=8453, market_unique_key="0x...")
```

### Rewards (Merkl + Morpho URD)

```python
success, rewards = await adapter.get_claimable_rewards(chain_id=8453)
success, txs = await adapter.claim_rewards(chain_id=8453, claim_merkl=True, claim_urd=True)
```

### Vaults (MetaMorpho / ERC-4626)

```python
success, vaults = await adapter.get_all_vaults(chain_id=8453, include_v2=True)
success, tx = await adapter.vault_deposit(chain_id=8453, vault_address="0x...", assets=123)
success, tx = await adapter.vault_withdraw(chain_id=8453, vault_address="0x...", assets=123)
success, tx = await adapter.vault_mint(chain_id=8453, vault_address="0x...", shares=123)
success, tx = await adapter.vault_redeem(chain_id=8453, vault_address="0x...", shares=123)
```

### Operator auth + Public Allocator (optional)

```python
success, tx = await adapter.set_authorization(chain_id=8453, authorized="0xBUNDLER...", is_authorized=True)
success, tx = await adapter.borrow_with_jit_liquidity(chain_id=8453, market_unique_key="0x...", qty=123, atomic=True)
```

## Return Format

All methods return `(success: bool, data: Any)` tuples.

## Testing

```bash
poetry run pytest wayfinder_paths/adapters/morpho_adapter/ -v
```
