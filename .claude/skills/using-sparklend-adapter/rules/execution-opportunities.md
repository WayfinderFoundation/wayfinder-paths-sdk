# SparkLend execution (supply/withdraw/borrow/repay/collateral/rewards)

## Safety

- Prefer running the existing fork simulation first:
  - `poetry run pytest wayfinder_paths/adapters/sparklend_adapter/test_gorlami_simulation.py -v`
- For real transactions, use MCP `core_execute(...)` so the review hooks can show a preview.

## Primary execution surface

- Adapter: `wayfinder_paths/adapters/sparklend_adapter/adapter.py`

## Supported write methods

### Supply / withdraw

- `lend(chain_id, underlying_token, qty)`
- `unlend(chain_id, underlying_token, qty, withdraw_full=False)`
- `qty` is a raw integer amount in token base units.
- For native supply/withdraw, pass `ZERO_ADDRESS` as `underlying_token`.
- Native `lend(...)` returns `{ "wrap_tx", "supply_tx" }`.
- Native `unlend(...)` returns `{ "withdraw_tx", "unwrap_tx" }`.

### Borrow / repay

- `borrow(chain_id, asset, amount, rate_mode=VARIABLE_RATE_MODE)`
- `repay(chain_id, asset, amount, rate_mode=VARIABLE_RATE_MODE, repay_full=False)`
- SparkLend supports both:
  - `VARIABLE_RATE_MODE = 2`
  - `STABLE_RATE_MODE = 1`
- Stable borrow/repay is only valid if the reserve has `stable_borrow_rate_enabled`.

### Native borrow / native repay

- `borrow_native(chain_id, amount, rate_mode=VARIABLE_RATE_MODE)`
- `repay_native(chain_id, amount, rate_mode=VARIABLE_RATE_MODE, repay_full=False)`
- These are the dedicated native-token borrow/repay helpers.

### Collateral / rewards

- `set_collateral(chain_id, underlying_token, use_as_collateral=True)`
- `claim_rewards(chain_id)`
- `claim_rewards(...)` derives incentivized reserve token addresses automatically before building the tx.
- If no incentivized assets are found, `claim_rewards(...)` returns a successful no-op payload like `{ "claimed": [], "note": "no incentivized assets found" }` instead of a tx hash.

## Common flows

### Supply native, enable collateral, borrow ERC20

```python
"""Supply native collateral on SparkLend and borrow USDC."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.sparklend_adapter.adapter import SparkLendAdapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_ETHEREUM
from wayfinder_paths.core.constants.contracts import ZERO_ADDRESS

USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"

async def main():
    adapter = await get_adapter(SparkLendAdapter, "main")  # wallet required

    ok, tx = await adapter.lend(
        chain_id=CHAIN_ID_ETHEREUM,
        underlying_token=ZERO_ADDRESS,
        qty=50_000_000_000_000_000,  # 0.05 ETH in wei
    )
    if not ok:
        raise RuntimeError(tx)

    wrapped = await adapter._wrapped_native(chain_id=CHAIN_ID_ETHEREUM)
    ok, tx = await adapter.set_collateral(
        chain_id=CHAIN_ID_ETHEREUM,
        underlying_token=wrapped,
        use_as_collateral=True,
    )
    if not ok:
        raise RuntimeError(tx)

    ok, tx = await adapter.borrow(
        chain_id=CHAIN_ID_ETHEREUM,
        asset=USDC,
        amount=10 * 10**6,
    )
    if not ok:
        raise RuntimeError(tx)

if __name__ == "__main__":
    asyncio.run(main())
```

### Repay full and withdraw full

```python
ok, tx = await adapter.repay(
    chain_id=CHAIN_ID_ETHEREUM,
    asset=USDC,
    amount=0,
    repay_full=True,
)

ok, tx = await adapter.unlend(
    chain_id=CHAIN_ID_ETHEREUM,
    underlying_token=ZERO_ADDRESS,
    qty=0,
    withdraw_full=True,
)
```

### Claim rewards

```python
ok, tx = await adapter.claim_rewards(chain_id=CHAIN_ID_ETHEREUM)
```
