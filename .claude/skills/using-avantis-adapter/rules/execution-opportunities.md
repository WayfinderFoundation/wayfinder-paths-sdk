# Avantis execution (ERC-4626 deposit + redeem shares)

## Safety

- Prefer running the existing fork simulation first:
  - `poetry run pytest wayfinder_paths/adapters/avantis_adapter/test_gorlami_simulation.py -v`
  - Note: this test requires the gorlami fork proxy to be configured and reachable (see `TESTING.md`).
- For real transactions, use MCP `run_script(...)` so the safety review hook can show a preview (adapter-based contract interactions run inside a Python script; `execute(...)` doesn’t cover them).

## Execution pattern (wallet required)

Avantis write operations require:
- `wallet_address`
- `sign_callback`

This adapter uses standard args, so `get_adapter(AvantisAdapter, wallet_label)` works:

```python
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.avantis_adapter import AvantisAdapter

adapter = get_adapter(AvantisAdapter, "main")
```

## Common flows (adapter methods)

### Deposit USDC (assets → shares)

```python
"""Deposit USDC into Avantis avUSDC vault (ERC-4626 deposit)."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.avantis_adapter import AvantisAdapter
from wayfinder_paths.core.constants.contracts import AVANTIS_AVUSDC, BASE_USDC

AMOUNT_USDC = 10 * 10**6  # 10 USDC (6 decimals)

async def main():
    adapter = get_adapter(AvantisAdapter, "main")
    ok, tx = await adapter.deposit(vault_address=AVANTIS_AVUSDC, underlying_token=BASE_USDC, amount=AMOUNT_USDC)
    if not ok:
        raise RuntimeError(tx)
    print("tx=", tx)

if __name__ == "__main__":
    asyncio.run(main())
```

### Redeem all shares (shares → assets)

```python
"""Redeem all avUSDC shares back to USDC (ERC-4626 redeem)."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.avantis_adapter import AvantisAdapter
from wayfinder_paths.core.constants.contracts import AVANTIS_AVUSDC

async def main():
    adapter = get_adapter(AvantisAdapter, "main")
    ok, tx = await adapter.withdraw(vault_address=AVANTIS_AVUSDC, amount=0, redeem_full=True)
    if not ok:
        raise RuntimeError(tx)
    print("tx=", tx)  # may be "no shares to redeem" if position is empty

if __name__ == "__main__":
    asyncio.run(main())
```

## Key execution methods

| Method | Purpose | Notes |
|--------|---------|-------|
| `deposit(vault_address?, underlying_token?, amount)` | Deposit underlying (USDC) | `amount` is **assets** (USDC raw units); may send an ERC20 approval tx first |
| `withdraw(vault_address?, amount, redeem_full?)` | Redeem shares (avUSDC) | `amount` is **shares** (avUSDC raw units); `redeem_full=True` auto-finds max shares |
