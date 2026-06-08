# Morpho Adapter (Morpho Markets / Vaults)

Adapter for Morpho Markets and Morpho Vaults. Unlike pool-style lending protocols, Morpho market actions are **market-specific**: every market is identified by a `marketId` (bytes32 hex string) and has immutable parameters `(loanToken, collateralToken, oracle, irm, lltv)`. The adapter keeps the Python argument name `market_unique_key` for backward compatibility, but it is sent to the current Morpho API as `marketId`.

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

Returns market snapshots including `marketId` (also mirrored as `uniqueKey` for older callers), loan/collateral assets, `lltv`, oracle/IRM addresses, warnings, public-allocator liquidity, and point-in-time `supply_apy` / `borrow_apy` from Morpho's API.

### get_full_user_state / get_full_user_state_per_chain

Queries all Morpho chains (or a single chain) and returns the user's positions. Each position
includes market-level APY and reward data from the Morpho API:

| Field | Description |
|-------|-------------|
| `supply_apy` | Base supply APY |
| `net_supply_apy` | Net supply APY (after protocol fees) |
| `borrow_apy` | Base borrow APY |
| `net_borrow_apy` | Net borrow APY |
| `reward_supply_apr` | Total incentive APR on supply side |
| `reward_borrow_apr` | Total incentive APR on borrow side |

```python
success, state = await adapter.get_full_user_state(account="0x...")
for pos in state["positions"]:
    print(pos["marketId"], pos["supply_apy"], pos["reward_supply_apr"])
```

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

### Rewards (Merkl current, URD legacy)

```python
success, rewards = await adapter.get_claimable_rewards(chain_id=8453)
success, txs = await adapter.claim_rewards(chain_id=8453)
```

Current Morpho reward programs are Merkl-distributed, so Merkl reads/claims are enabled by default. Legacy URD claims remain available through `claim_urd_rewards(...)` or `claim_rewards(..., claim_urd=True)` for historical distributions, but the old `rewards.morpho.org` JSON API is deprecated by Morpho and should not be treated as a current rewards source.

### Vaults (MetaMorpho / ERC-4626)

```python
success, vaults = await adapter.get_all_vaults(chain_id=8453, include_v2=True)
success, tx = await adapter.vault_deposit(chain_id=8453, vault_address="0x...", assets=123)
success, tx = await adapter.vault_withdraw(chain_id=8453, vault_address="0x...", assets=123)
success, tx = await adapter.vault_mint(chain_id=8453, vault_address="0x...", shares=123)
success, tx = await adapter.vault_redeem(chain_id=8453, vault_address="0x...", shares=123)
```

Vault reads include Vault V1 and Vault V2. Vault V2 entries include API fields such as `avg_net_apy_excluding_rewards`, `share_price`, `idle_assets`, `liquidity`, and adapter allocation data. Vault writes use direct ERC-4626 calls. Morpho's current TypeScript SDK routes some Vault V2 deposit flows through Bundler3 with share-price slippage checks and native-token wrapping; use those SDK/bundler flows when you need that protection or native wrapping.

### Operator auth + Public Allocator (optional)

```python
success, tx = await adapter.set_authorization(chain_id=8453, authorized="0xBUNDLER...", is_authorized=True)
success, tx = await adapter.borrow_with_jit_liquidity(chain_id=8453, market_unique_key="0x...", qty=123, atomic=True)
```

Public Allocator support uses `publicAllocatorSharedLiquidity` and `reallocatableLiquidityAssets` from the Morpho API. The adapter's bundled JIT path targets the legacy bytes-array Morpho bundler style when a compatible `bundler_address` is configured. Current Bundler3 integrations use `multicall(Call[])` plus adapter contracts, so treat full Bundler3 migration as a separate integration task.

## Return Format

All methods return `(success: bool, data: Any)` tuples.

## Testing

```bash
poetry run pytest wayfinder_paths/adapters/morpho_adapter/ -v
```
