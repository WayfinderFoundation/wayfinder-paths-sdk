# EigenCloud execution (deposit/delegate/withdraw/claim)

## Safety rails

- Use `get_adapter(EigenCloudAdapter, wallet_label="main")` so the adapter has `wallet_address` + `sign_callback`.
- If any step returns `ok=False`, stop and report the error (don’t continue a multi-step flow).
- Amounts are raw integers (token units or share units). Handle decimals explicitly.

## Primary data source

- Adapter: `wayfinder_paths/adapters/eigencloud_adapter/adapter.py`

## Execution scripts

All execution scripts go under `.wayfinder_runs/`.

### Deposit into a strategy (restake)

```python
"""Deposit (restake) an underlying token into an EigenLayer strategy."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.eigencloud_adapter import EigenCloudAdapter

STRATEGY = "0x0000000000000000000000000000000000000000"

async def main() -> None:
    adapter = get_adapter(EigenCloudAdapter, wallet_label="main")
    ok, out = await adapter.deposit(strategy=STRATEGY, amount=123_000_000)  # raw units
    if not ok:
        raise RuntimeError(out)
    print(out)  # includes tx_hash + (optional) approve_tx_hash

if __name__ == "__main__":
    asyncio.run(main())
```

### Delegate to an operator

```python
"""Delegate restaked position to an EigenLayer operator."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.eigencloud_adapter import EigenCloudAdapter

OPERATOR = "0x0000000000000000000000000000000000000000"

async def main() -> None:
    adapter = get_adapter(EigenCloudAdapter, wallet_label="main")
    ok, tx_hash = await adapter.delegate(operator=OPERATOR)
    if not ok:
        raise RuntimeError(tx_hash)
    print("tx=", tx_hash)

if __name__ == "__main__":
    asyncio.run(main())
```

### Queue withdrawals (creates withdrawal roots)

```python
"""Queue withdrawals by strategy + deposit shares (share units)."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.eigencloud_adapter import EigenCloudAdapter

STRATEGIES = ["0x0000000000000000000000000000000000000000"]
DEPOSIT_SHARES = [10**18]  # share units for each strategy

async def main() -> None:
    adapter = get_adapter(EigenCloudAdapter, wallet_label="main")
    ok, out = await adapter.queue_withdrawals(
        strategies=STRATEGIES,
        deposit_shares=DEPOSIT_SHARES,
        include_withdrawal_roots=True,
    )
    if not ok:
        raise RuntimeError(out)
    print(out)  # {"tx_hash": "...", "withdrawal_roots": [...]}

if __name__ == "__main__":
    asyncio.run(main())
```

### Complete a queued withdrawal (by withdrawal root)

```python
"""Complete a queued withdrawal (after the delay window) by withdrawal root."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.eigencloud_adapter import EigenCloudAdapter

WITHDRAWAL_ROOT = "0x" + ("00" * 32)

async def main() -> None:
    adapter = get_adapter(EigenCloudAdapter, wallet_label="main")
    ok, out = await adapter.complete_withdrawal(
        withdrawal_root=WITHDRAWAL_ROOT,
        receive_as_tokens=True,
        tokens_override=None,  # rarely needed; adapter resolves underlying tokens
    )
    if not ok:
        raise RuntimeError(out)
    print(out)

if __name__ == "__main__":
    asyncio.run(main())
```

### Claim rewards (requires offchain claim structs)

```python
"""Claim EigenLayer rewards using an offchain-prepared claim struct."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.eigencloud_adapter import EigenCloudAdapter

CLAIM = {}  # fill from EigenLayer app/CLI/indexer (do not guess)
RECIPIENT = "0x0000000000000000000000000000000000000000"

async def main() -> None:
    adapter = get_adapter(EigenCloudAdapter, wallet_label="main")
    ok, tx_hash = await adapter.claim_rewards(claim=CLAIM, recipient=RECIPIENT)
    if not ok:
        raise RuntimeError(tx_hash)
    print("tx=", tx_hash)

if __name__ == "__main__":
    asyncio.run(main())
```

