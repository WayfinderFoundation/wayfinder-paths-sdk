# Aave V3 execution (supply/withdraw/borrow/repay/collateral/rewards)

## Safety

- Prefer running the existing fork simulations first:
  - `poetry run pytest wayfinder_paths/adapters/aave_v3_adapter/test_gorlami_simulation.py -v`
- For real transactions, use MCP `execute(...)` so the safety review hook can show a preview.

## Common flows (adapter methods)

### Supply + enable collateral + borrow (variable rate)

```python
"""Supply collateral then borrow on Aave V3."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.aave_v3_adapter import AaveV3Adapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_ARBITRUM
from wayfinder_paths.core.constants.contracts import ARBITRUM_USDC

async def main():
    adapter = get_adapter(AaveV3Adapter, "main")  # wallet required for signing

    ok, tx = await adapter.lend(chain_id=CHAIN_ID_ARBITRUM, underlying_token=ARBITRUM_USDC, qty=10 * 10**6)
    if not ok:
        raise RuntimeError(tx)

    ok, tx = await adapter.set_collateral(chain_id=CHAIN_ID_ARBITRUM, underlying_token=ARBITRUM_USDC, use_as_collateral=True)
    if not ok:
        raise RuntimeError(tx)

    # Borrow some other asset by address (example placeholder)
    ok, tx = await adapter.borrow(chain_id=CHAIN_ID_ARBITRUM, underlying_token="0x...", qty=1)
    if not ok:
        raise RuntimeError(tx)

if __name__ == "__main__":
    asyncio.run(main())
```

### Repay full + withdraw full

```python
ok, tx = await adapter.repay(chain_id=CHAIN_ID_ARBITRUM, underlying_token="0x...", qty=0, repay_full=True)
ok, tx = await adapter.unlend(chain_id=CHAIN_ID_ARBITRUM, underlying_token=ARBITRUM_USDC, qty=0, withdraw_full=True)
```

### Claim rewards

```python
# If assets is omitted, the adapter derives incentivized aToken/debt-token addresses via UiIncentiveDataProviderV3.
ok, tx = await adapter.claim_all_rewards(chain_id=CHAIN_ID_ARBITRUM)
```
