# Compound execution (base flows + collateral + rewards)

## Safety

- Prefer running the existing fork simulation first:
  - `poetry run pytest wayfinder_paths/adapters/compound_adapter/test_gorlami_simulation.py -v`
- For real transactions, use the tracked script flow so wallet activity is associated with the session.

## Primary execution surface

- Adapter: `wayfinder_paths/adapters/compound_adapter/adapter.py`

## Supported write methods

### Base supply / base withdraw

- `lend(chain_id, comet, base_token, amount)`
- `unlend(chain_id, comet, base_token, amount, withdraw_full=False)`
- These methods operate on the market's **base asset** only.
- `base_token` must match the Comet's on-chain `baseToken()` or the adapter returns an error.
- `amount` is a raw integer amount in the base token's units.
- `unlend(..., withdraw_full=True)` allows `amount=0` and uses Compound's full-withdraw sentinel.

### Base borrow / base repay

- `borrow(chain_id, comet, base_token, amount)`
- `repay(chain_id, comet, base_token, amount, repay_full=False)`
- These methods also operate on the market's **base asset** only.
- `borrow(...)` enforces the market's `base_borrow_min`.
- `repay(..., repay_full=True)` allows `amount=0` and uses Compound's full-repay sentinel.

### Collateral supply / collateral withdraw

- `supply_collateral(chain_id, comet, collateral_asset, amount)`
- `withdraw_collateral(chain_id, comet, collateral_asset, amount, withdraw_full=False)`
- `collateral_asset` must be one of the Comet's supported collateral assets.
- `amount` is a raw integer amount in that collateral token's units.
- `withdraw_collateral(..., withdraw_full=True)` reads the exact `collateralBalanceOf(...)` and withdraws that amount.

### Rewards

- `claim_rewards(chain_id, comet, rewards_contract=None, should_accrue=True)`
- If `rewards_contract` is omitted, the adapter uses the configured rewards contract for that Comet.
- `claim_rewards(...)` returns a tx hash on success; it does not return a claimed-amount summary.

## Common flows

### Supply base asset

```python
"""Supply the base asset to a Compound Comet market."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.compound_adapter import CompoundAdapter

CHAIN_ID = 8453
COMET = "0xb125E6687d4313864e53df431d5425969c15Eb2F"  # Base USDC Comet
BASE_TOKEN = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # Base USDC
AMOUNT = 10 * 10**6

async def main():
    adapter = await get_adapter(CompoundAdapter, "main")
    ok, tx = await adapter.lend(
        chain_id=CHAIN_ID,
        comet=COMET,
        base_token=BASE_TOKEN,
        amount=AMOUNT,
    )
    if not ok:
        raise RuntimeError(tx)
    print(tx)

if __name__ == "__main__":
    asyncio.run(main())
```

### Supply collateral, then borrow the base asset

```python
"""Supply collateral to Compound, then borrow the base asset."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.compound_adapter import CompoundAdapter

CHAIN_ID = 8453
COMET = "0xb125E6687d4313864e53df431d5425969c15Eb2F"  # Base USDC Comet
BASE_TOKEN = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
COLLATERAL = "0x4200000000000000000000000000000000000006"  # WETH on Base
COLLATERAL_AMOUNT = 10**16  # 0.01 WETH
BORROW_AMOUNT = 5 * 10**6  # 5 USDC

async def main():
    adapter = await get_adapter(CompoundAdapter, "main")

    ok, tx = await adapter.supply_collateral(
        chain_id=CHAIN_ID,
        comet=COMET,
        collateral_asset=COLLATERAL,
        amount=COLLATERAL_AMOUNT,
    )
    if not ok:
        raise RuntimeError(tx)

    ok, tx = await adapter.borrow(
        chain_id=CHAIN_ID,
        comet=COMET,
        base_token=BASE_TOKEN,
        amount=BORROW_AMOUNT,
    )
    if not ok:
        raise RuntimeError(tx)

    print(tx)

if __name__ == "__main__":
    asyncio.run(main())
```

### Repay full, withdraw collateral full, and unlend full

```python
"""Close out a Compound position using the full flags."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.compound_adapter import CompoundAdapter

CHAIN_ID = 8453
COMET = "0xb125E6687d4313864e53df431d5425969c15Eb2F"
BASE_TOKEN = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
COLLATERAL = "0x4200000000000000000000000000000000000006"

async def main():
    adapter = await get_adapter(CompoundAdapter, "main")

    ok, tx = await adapter.repay(
        chain_id=CHAIN_ID,
        comet=COMET,
        base_token=BASE_TOKEN,
        amount=0,
        repay_full=True,
    )
    if not ok:
        raise RuntimeError(tx)

    ok, tx = await adapter.withdraw_collateral(
        chain_id=CHAIN_ID,
        comet=COMET,
        collateral_asset=COLLATERAL,
        amount=0,
        withdraw_full=True,
    )
    if not ok:
        raise RuntimeError(tx)

    ok, tx = await adapter.unlend(
        chain_id=CHAIN_ID,
        comet=COMET,
        base_token=BASE_TOKEN,
        amount=0,
        withdraw_full=True,
    )
    if not ok:
        raise RuntimeError(tx)

    print(tx)

if __name__ == "__main__":
    asyncio.run(main())
```

### Claim rewards

```python
"""Claim Compound rewards for one Comet market."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.compound_adapter import CompoundAdapter

CHAIN_ID = 8453
COMET = "0xb125E6687d4313864e53df431d5425969c15Eb2F"

async def main():
    adapter = await get_adapter(CompoundAdapter, "main")
    ok, tx = await adapter.claim_rewards(
        chain_id=CHAIN_ID,
        comet=COMET,
    )
    if not ok:
        raise RuntimeError(tx)
    print(tx)

if __name__ == "__main__":
    asyncio.run(main())
```
