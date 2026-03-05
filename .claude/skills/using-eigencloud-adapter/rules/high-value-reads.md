# EigenCloud reads (strategies + positions + rewards metadata)

## Data accuracy (no guessing)

- Do **not** invent shares, balances, operator addresses, or claimability.
- Only report values fetched via the adapter.
- Adapter read methods return `(ok, data)` tuples — always destructure and handle `ok=False`.

## Primary data source

- Adapter: `wayfinder_paths/adapters/eigencloud_adapter/adapter.py`
- Addresses/constants: `wayfinder_paths/core/constants/contracts.py` (EigenCloud entries)

Notes:
- EigenCloud adapter is **Ethereum mainnet only** (chain id is implicit in the adapter).
- Amounts are raw integers (token units / share units). You must handle decimals when presenting human units.

## Ad-hoc read scripts

All ad-hoc scripts go under `.wayfinder_runs/` and use `get_adapter()`.

### List strategies (“markets”)

```python
"""List EigenCloud strategies and their underlying tokens."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.eigencloud_adapter import EigenCloudAdapter

async def main() -> None:
    adapter = get_adapter(EigenCloudAdapter)  # read-only
    ok, markets = await adapter.get_all_markets(include_total_shares=True, include_share_to_underlying=True)
    if not ok:
        raise RuntimeError(markets)
    for m in markets:
        print(m.get("strategy_name"), m.get("strategy"), m.get("underlying_symbol"))

if __name__ == "__main__":
    asyncio.run(main())
```

### User positions (deposited + withdrawable shares; optional USD)

```python
"""Fetch a user’s EigenCloud positions (shares + optional USD estimates)."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.eigencloud_adapter import EigenCloudAdapter

USER = "0x0000000000000000000000000000000000000000"

async def main() -> None:
    adapter = get_adapter(EigenCloudAdapter)
    ok, pos = await adapter.get_pos(account=USER, include_usd=False)
    if not ok:
        raise RuntimeError(pos)
    print(pos)

if __name__ == "__main__":
    asyncio.run(main())
```

### Delegation state + rewards metadata

```python
"""Fetch delegation state and rewards metadata for a user."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.eigencloud_adapter import EigenCloudAdapter

USER = "0x0000000000000000000000000000000000000000"

async def main() -> None:
    adapter = get_adapter(EigenCloudAdapter)

    ok, delegation = await adapter.get_delegation_state(account=USER)
    if not ok:
        raise RuntimeError(delegation)
    print("delegation=", delegation)

    ok, rewards = await adapter.get_rewards_metadata(account=USER)
    if not ok:
        raise RuntimeError(rewards)
    print("rewards_metadata=", rewards)

if __name__ == "__main__":
    asyncio.run(main())
```

### Full user state (positions + delegation + queued withdrawals + rewards metadata)

```python
"""Aggregate positions + delegation + queued withdrawals + rewards metadata."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.eigencloud_adapter import EigenCloudAdapter

USER = "0x0000000000000000000000000000000000000000"
WITHDRAWAL_ROOTS = []  # capture from `queue_withdrawals(...)`/`undelegate(...)` results, or fetch via tx hash

async def main() -> None:
    adapter = get_adapter(EigenCloudAdapter)
    ok, state = await adapter.get_full_user_state(
        account=USER,
        include_usd=False,
        include_queued_withdrawals=True,
        withdrawal_roots=WITHDRAWAL_ROOTS,
        include_rewards_metadata=True,
    )
    if not ok:
        raise RuntimeError(state)
    print(state)

if __name__ == "__main__":
    asyncio.run(main())
```

## Key read methods

| Method | Purpose | Wallet needed? |
|--------|---------|----------------|
| `get_all_markets(...)` | List strategies + underlying token metadata | No |
| `get_pos(account?, include_usd?)` | Deposited shares + withdrawable shares by strategy | No |
| `get_delegation_state(account?)` | Delegation + operator approver | No |
| `get_rewards_metadata(account?)` | Current distribution root + claimer | No |
| `get_full_user_state(account, withdrawal_roots?, ...)` | Aggregated snapshot (you supply withdrawal roots) | No |
| `get_withdrawal_roots_from_tx_hash(tx_hash)` | Extract withdrawal roots from a tx receipt | No |
| `get_queued_withdrawal(withdrawal_root)` | Read queued withdrawal struct | No |

