# Morpho execution (markets + vaults + rewards)

## Safety

- Prefer running the existing fork simulations first:
  - `poetry run pytest wayfinder_paths/adapters/morpho_adapter/test_gorlami_simulation.py -v`
- Market operations are **market-specific**: choose a Morpho `marketId` and pass it as `market_unique_key`.
- Vault operations are **vault-specific**: choose a `vault_address`; do not pass a market id to vault methods.

## Common flows (adapter methods)

### Deposit collateral → supply → borrow

```python
ok, tx = await adapter.supply_collateral(chain_id=8453, market_unique_key="0x...", qty=123)
ok, tx = await adapter.lend(chain_id=8453, market_unique_key="0x...", qty=123)
ok, tx = await adapter.borrow(chain_id=8453, market_unique_key="0x...", qty=123)
```

### Full close (shares-based)

```python
ok, tx = await adapter.repay(chain_id=8453, market_unique_key="0x...", qty=0, repay_full=True)
ok, tx = await adapter.unlend(chain_id=8453, market_unique_key="0x...", qty=0, withdraw_full=True)
```

### Claim rewards

```python
ok, txs = await adapter.claim_rewards(chain_id=8453)
```

Merkl is the current default rewards path. Historical URD claims are opt-in with `claim_urd=True` and depend on legacy distribution data.

### Vault ops (ERC-4626)

```python
ok, tx = await adapter.vault_deposit(chain_id=8453, vault_address="0x...", assets=123)
ok, tx = await adapter.vault_withdraw(chain_id=8453, vault_address="0x...", assets=123)
ok, tx = await adapter.vault_mint(chain_id=8453, vault_address="0x...", shares=123)
ok, tx = await adapter.vault_redeem(chain_id=8453, vault_address="0x...", shares=123)
```

Direct adapter vault writes call ERC-4626 methods. Morpho's current Vault V2 SDK can route deposits through Bundler3 with share-price slippage checks and native-token wrapping; use that path when those protections are required.

### Public Allocator JIT liquidity (optional)

```python
# If `atomic=True` and a bundler address is configured, the adapter attempts to bundle reallocate + borrow.
ok, tx = await adapter.borrow_with_jit_liquidity(
    chain_id=8453,
    market_unique_key="0x...",
    qty=123,
    atomic=True,
)
```

The adapter can use Public Allocator shared liquidity from the API. Its bundled JIT path expects a compatible legacy bytes-array Morpho bundler address; current Bundler3 uses `multicall(Call[])` plus adapter contracts and is a separate integration.
