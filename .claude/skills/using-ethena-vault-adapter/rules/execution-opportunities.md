# Ethena sUSDe execution (deposit + cooldown withdraw + unstake)

## Safety

- Prefer running the existing fork simulation first:
  - `poetry run pytest wayfinder_paths/adapters/ethena_vault_adapter/test_gorlami_simulation.py -v`
- For real transactions, use MCP `run_script(...)` so the safety review hook can show a preview (these flows execute inside a Python script; `execute(...)` doesn’t cover them).

## Common flows (adapter methods)

### Deposit (stake USDe → receive sUSDe shares)

```python
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.ethena_vault_adapter import EthenaVaultAdapter

AMOUNT_USDE_WEI = 100 * 10**18

async def main():
    adapter = await get_adapter(EthenaVaultAdapter, "main")  # wallet required for signing

    ok, tx = await adapter.deposit_usde(amount_assets=AMOUNT_USDE_WEI)
    if not ok:
        raise RuntimeError(tx)
    print("tx=", tx)

if __name__ == "__main__":
    asyncio.run(main())
```

### Withdraw (two-step)

Step 1: start cooldown (pick one)

```python
ok, tx = await adapter.request_withdraw_by_shares(shares=50 * 10**18)
# or:
ok, tx = await adapter.request_withdraw_by_assets(assets=100 * 10**18)
```

Step 2: after cooldown expires, claim (unstake)

```python
ok, tx = await adapter.claim_withdraw(require_matured=True)
```

## Key execution methods

| Method | Purpose | Notes |
|--------|---------|-------|
| `deposit_usde(amount_assets, receiver?)` | Stake USDe into the mainnet sUSDe vault | May send an ERC20 approval tx first |
| `request_withdraw_by_shares(shares)` | Start cooldown by sUSDe share amount | Mainnet-only |
| `request_withdraw_by_assets(assets)` | Start cooldown by USDe asset amount | Mainnet-only |
| `claim_withdraw(receiver?, require_matured?)` | Claim (unstake) after cooldown | Returns `(True, "no pending cooldown")` if nothing is pending |
