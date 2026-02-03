# Moonwell execution (ad-hoc scripts)

## Execution pattern

All write operations use ad-hoc scripts under `.wayfinder_runs/`:

1. Write script with `get_adapter(MoonwellAdapter, "wallet_label")`
2. Run via `mcp__wayfinder__run_script(script_path, wallet_label)`

## Supply (lend) USDC

```python
"""Supply USDC to Moonwell."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.moonwell_adapter import MoonwellAdapter

USDC_MTOKEN = "0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22"
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
AMOUNT = 10_000_000  # 10 USDC (6 decimals)

async def main():
    adapter = get_adapter(MoonwellAdapter, "main")
    ok, result = await adapter.lend(mtoken=USDC_MTOKEN, underlying_token=BASE_USDC, amount=AMOUNT)
    print(f"Success: {ok}, TX: {result}")

if __name__ == "__main__":
    asyncio.run(main())
```

## Withdraw (unlend)

**Important:** `unlend()` calls `redeem()` which expects **mToken amount**, not underlying amount.
Use `max_withdrawable_mtoken()` to get the correct amount.

```python
"""Withdraw USDC from Moonwell."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.moonwell_adapter import MoonwellAdapter

USDC_MTOKEN = "0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22"

async def main():
    adapter = get_adapter(MoonwellAdapter, "main")

    # Get max withdrawable (returns dict with mToken and underlying amounts)
    ok, info = await adapter.max_withdrawable_mtoken(mtoken=USDC_MTOKEN)
    print(f"Max withdrawable: {info['underlying']:.6f} USDC")
    print(f"mTokens to redeem: {info['cTokens_raw']}")

    # unlend() takes mToken amount, not underlying
    ok, result = await adapter.unlend(mtoken=USDC_MTOKEN, amount=info['cTokens_raw'])
    print(f"Success: {ok}, TX: {result}")

if __name__ == "__main__":
    asyncio.run(main())
```

## Enable collateral

```python
"""Enable supplied asset as collateral."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.moonwell_adapter import MoonwellAdapter

USDC_MTOKEN = "0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22"

async def main():
    adapter = get_adapter(MoonwellAdapter, "main")
    ok, result = await adapter.set_collateral(mtoken=USDC_MTOKEN)
    print(f"Success: {ok}, TX: {result}")

if __name__ == "__main__":
    asyncio.run(main())
```

## Borrow

```python
"""Borrow WETH against collateral."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.moonwell_adapter import MoonwellAdapter

WETH_MTOKEN = "0x628ff693426583D9a7FB391E54366292F509D457"
AMOUNT = 10**16  # 0.01 WETH (18 decimals)

async def main():
    adapter = get_adapter(MoonwellAdapter, "main")
    # get_borrowable_amount returns account liquidity in USD (no mtoken param)
    ok, liquidity_usd = await adapter.get_borrowable_amount()
    print(f"Account liquidity: ${liquidity_usd / 1e18:.2f}")

    # Note: liquidity is in USD, you'll need to convert to asset units
    # For simplicity, just check if there's any liquidity
    if liquidity_usd > 0:
        ok, result = await adapter.borrow(mtoken=WETH_MTOKEN, amount=AMOUNT)
        print(f"Success: {ok}, TX: {result}")
    else:
        print("Insufficient collateral")

if __name__ == "__main__":
    asyncio.run(main())
```

## Repay

```python
"""Repay borrowed WETH."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.moonwell_adapter import MoonwellAdapter

WETH_MTOKEN = "0x628ff693426583D9a7FB391E54366292F509D457"
BASE_WETH = "0x4200000000000000000000000000000000000006"
AMOUNT = 10**16  # 0.01 WETH

async def main():
    adapter = get_adapter(MoonwellAdapter, "main")
    ok, result = await adapter.repay(mtoken=WETH_MTOKEN, underlying_token=BASE_WETH, amount=AMOUNT)
    print(f"Success: {ok}, TX: {result}")

if __name__ == "__main__":
    asyncio.run(main())
```

## Claim WELL rewards

```python
"""Claim pending WELL rewards."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.moonwell_adapter import MoonwellAdapter

async def main():
    adapter = get_adapter(MoonwellAdapter, "main")
    # claim_rewards returns dict of claimed rewards, not tx hash
    ok, rewards = await adapter.claim_rewards(min_rewards_usd=0.0)
    print(f"Success: {ok}, Rewards: {rewards}")
    # rewards = {"base_0x...": 123456789, ...} (token_key: raw_amount)

if __name__ == "__main__":
    asyncio.run(main())
```

## Wrap ETH to WETH

```python
"""Wrap ETH to WETH on Base."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.moonwell_adapter import MoonwellAdapter

AMOUNT = 10**16  # 0.01 ETH

async def main():
    adapter = get_adapter(MoonwellAdapter, "main")
    ok, result = await adapter.wrap_eth(amount=AMOUNT)
    print(f"Success: {ok}, TX: {result}")

if __name__ == "__main__":
    asyncio.run(main())
```

## Key execution methods

| Method | Purpose | Params |
|--------|---------|--------|
| `lend(mtoken, underlying_token, amount)` | Supply underlying | amount in raw units |
| `unlend(mtoken, amount)` | Withdraw underlying | amount in raw units |
| `borrow(mtoken, amount)` | Borrow against collateral | amount in raw units |
| `repay(mtoken, underlying_token, amount, repay_full=False)` | Repay borrow | amount in raw units |
| `set_collateral(mtoken)` | Enable as collateral | — |
| `remove_collateral(mtoken)` | Disable collateral | — |
| `claim_rewards(min_rewards_usd?)` | Claim WELL rewards | returns dict of claimed rewards |
| `wrap_eth(amount)` | Wrap ETH to WETH | amount in raw units (wei) |
