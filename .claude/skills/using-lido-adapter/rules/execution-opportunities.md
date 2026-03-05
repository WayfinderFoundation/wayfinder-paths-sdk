# Lido execution (stake/wrap/withdraw)

## Safety rails

- Use `get_adapter(LidoAdapter, wallet_label="main")` so the adapter has `wallet_address` + `sign_callback`.
- If any step returns `ok=False`, stop and report the error (don’t continue a multi-step flow).
- All amounts are raw integers (wei).

## Primary data source

- Adapter: `wayfinder_paths/adapters/lido_adapter/adapter.py`

## Execution scripts

All execution scripts go under `.wayfinder_runs/`.

### Stake ETH → stETH (1 tx)

```python
"""Stake ETH into Lido and receive stETH."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.lido_adapter import LidoAdapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_ETHEREUM

async def main() -> None:
    adapter = get_adapter(LidoAdapter, wallet_label="main")
    ok, tx_hash = await adapter.stake_eth(
        amount_wei=int(0.05 * 10**18),
        chain_id=CHAIN_ID_ETHEREUM,
        receive="stETH",
        check_limits=True,
    )
    if not ok:
        raise RuntimeError(tx_hash)
    print("tx=", tx_hash)

if __name__ == "__main__":
    asyncio.run(main())
```

### Stake ETH → wstETH (2 tx: stake + wrap)

```python
"""Stake ETH into Lido and receive wstETH (submit + wrap)."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.lido_adapter import LidoAdapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_ETHEREUM

async def main() -> None:
    adapter = get_adapter(LidoAdapter, wallet_label="main")
    ok, out = await adapter.stake_eth(
        amount_wei=int(0.05 * 10**18),
        chain_id=CHAIN_ID_ETHEREUM,
        receive="wstETH",
        check_limits=True,
    )
    if not ok:
        raise RuntimeError(out)
    print(out)  # {"stake_tx": "...", "wrap_tx": "...", ...}

if __name__ == "__main__":
    asyncio.run(main())
```

### Wrap stETH → wstETH (approval + wrap)

```python
"""Wrap stETH into wstETH."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.lido_adapter import LidoAdapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_ETHEREUM

async def main() -> None:
    adapter = get_adapter(LidoAdapter, wallet_label="main")
    ok, tx_hash = await adapter.wrap_steth(
        amount_steth_wei=int(0.10 * 10**18),
        chain_id=CHAIN_ID_ETHEREUM,
    )
    if not ok:
        raise RuntimeError(tx_hash)
    print("tx=", tx_hash)

if __name__ == "__main__":
    asyncio.run(main())
```

### Unwrap wstETH → stETH

```python
"""Unwrap wstETH into stETH."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.lido_adapter import LidoAdapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_ETHEREUM

async def main() -> None:
    adapter = get_adapter(LidoAdapter, wallet_label="main")
    ok, tx_hash = await adapter.unwrap_wsteth(
        amount_wsteth_wei=int(0.05 * 10**18),
        chain_id=CHAIN_ID_ETHEREUM,
    )
    if not ok:
        raise RuntimeError(tx_hash)
    print("tx=", tx_hash)

if __name__ == "__main__":
    asyncio.run(main())
```

### Request withdrawal (stETH or wstETH) → unstETH NFT (async)

```python
"""Request a Lido withdrawal (async)."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.lido_adapter import LidoAdapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_ETHEREUM

async def main() -> None:
    adapter = get_adapter(LidoAdapter, wallet_label="main")
    ok, out = await adapter.request_withdrawal(
        asset="stETH",
        amount_wei=int(0.10 * 10**18),
        owner=None,  # defaults to your wallet
        chain_id=CHAIN_ID_ETHEREUM,
    )
    if not ok:
        raise RuntimeError(out)
    print(out)  # {"tx": "...", "amounts": [...], "owner": "...", ...}

if __name__ == "__main__":
    asyncio.run(main())
```

### Claim withdrawals (when finalized)

```python
"""Claim finalized Lido withdrawals by request ID."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.lido_adapter import LidoAdapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_ETHEREUM

REQUEST_IDS = [1, 2, 3]

async def main() -> None:
    adapter = get_adapter(LidoAdapter, wallet_label="main")
    ok, tx_hash = await adapter.claim_withdrawals(
        request_ids=REQUEST_IDS,
        recipient=None,  # defaults to your wallet
        chain_id=CHAIN_ID_ETHEREUM,
    )
    if not ok:
        raise RuntimeError(tx_hash)
    print("tx=", tx_hash)

if __name__ == "__main__":
    asyncio.run(main())
```

