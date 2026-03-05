# Lido reads (rates + withdrawals + user state)

## Data accuracy (no guessing)

- Do **not** invent balances, rates, request IDs, or status fields.
- Only report values fetched via the adapter.
- Adapter read methods return `(ok, data)` tuples — always destructure and handle `ok=False`.

## Primary data source

- Adapter: `wayfinder_paths/adapters/lido_adapter/adapter.py`
- Contract addresses: `wayfinder_paths/core/constants/lido_contracts.py`

Notes:
- Lido staking/withdrawals are **Ethereum mainnet only** (`chain_id=1`).
- All amounts are raw integers (wei).

## Ad-hoc read scripts

All ad-hoc scripts go under `.wayfinder_runs/` and use `get_adapter()`.

### Rates (stETH per wstETH + inverse)

```python
"""Fetch Lido stETH/wstETH conversion rates (mainnet)."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.lido_adapter import LidoAdapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_ETHEREUM

async def main() -> None:
    adapter = get_adapter(LidoAdapter)  # read-only
    ok, rates = await adapter.get_rates(chain_id=CHAIN_ID_ETHEREUM)
    if not ok:
        raise RuntimeError(rates)
    print(rates)

if __name__ == "__main__":
    asyncio.run(main())
```

### Full user snapshot (balances + withdrawals + optional USD)

```python
"""Fetch a user’s Lido state: stETH/wstETH balances + withdrawal queue status."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.lido_adapter import LidoAdapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_ETHEREUM

USER = "0x0000000000000000000000000000000000000000"

async def main() -> None:
    adapter = get_adapter(LidoAdapter)
    ok, state = await adapter.get_full_user_state(
        account=USER,
        chain_id=CHAIN_ID_ETHEREUM,
        include_withdrawals=True,
        include_claimable=False,
        include_usd=False,
    )
    if not ok:
        raise RuntimeError(state)
    print(state)

if __name__ == "__main__":
    asyncio.run(main())
```

### Withdrawal requests + status

```python
"""List a user’s WithdrawalQueue request IDs and fetch their status."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.lido_adapter import LidoAdapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_ETHEREUM

USER = "0x0000000000000000000000000000000000000000"

async def main() -> None:
    adapter = get_adapter(LidoAdapter)
    ok, ids = await adapter.get_withdrawal_requests(account=USER, chain_id=CHAIN_ID_ETHEREUM)
    if not ok:
        raise RuntimeError(ids)
    print("request_ids=", ids)

    ok, statuses = await adapter.get_withdrawal_status(request_ids=ids, chain_id=CHAIN_ID_ETHEREUM)
    if not ok:
        raise RuntimeError(statuses)
    print("statuses=", statuses)

if __name__ == "__main__":
    asyncio.run(main())
```

## Key read methods

| Method | Purpose | Wallet needed? |
|--------|---------|----------------|
| `get_rates(chain_id=1)` | stETH↔wstETH conversion rates | No |
| `get_withdrawal_requests(account, chain_id=1)` | List a user’s request IDs | No |
| `get_withdrawal_status(request_ids, chain_id=1)` | Status for request IDs | No |
| `get_full_user_state(account, chain_id=1, include_withdrawals?, include_claimable?, include_usd?)` | Balances + withdrawal queue snapshot | No |

