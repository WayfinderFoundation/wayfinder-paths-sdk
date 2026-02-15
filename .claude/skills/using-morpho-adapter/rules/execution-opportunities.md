# Morpho execution (markets + vaults + rewards)

## Safety

- Prefer running the existing fork simulations first:
  - `poetry run pytest wayfinder_paths/adapters/morpho_adapter/test_gorlami_simulation.py -v`
- Market operations are **market-specific**: you must choose a `market_unique_key`.

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
ok, txs = await adapter.claim_rewards(chain_id=8453, claim_merkl=True, claim_urd=True)
```

### Vault ops (ERC-4626)

```python
ok, tx = await adapter.vault_deposit(chain_id=8453, vault_address="0x...", assets=123)
ok, tx = await adapter.vault_withdraw(chain_id=8453, vault_address="0x...", assets=123)
ok, tx = await adapter.vault_mint(chain_id=8453, vault_address="0x...", shares=123)
ok, tx = await adapter.vault_redeem(chain_id=8453, vault_address="0x...", shares=123)
```

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
