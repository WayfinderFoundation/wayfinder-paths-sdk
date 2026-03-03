# Avantis reads (avUSDC vault + vault manager + positions)

## Data accuracy (no guessing)

- Do **not** invent TVL, share price, rewards, or balances.
- Only report values fetched from Avantis contracts via the adapter.
- If an RPC call fails, respond with "unavailable" and provide the exact script/call to reproduce.

## Primary data source

- Adapter: `wayfinder_paths/adapters/avantis_adapter/adapter.py`
- Addresses: `wayfinder_paths/core/constants/contracts.py` (`AVANTIS_AVUSDC`, `AVANTIS_VAULT_MANAGER`, `BASE_USDC`)
- Chain: Base (`CHAIN_ID_BASE = 8453`)

## Ad-hoc read scripts

All read scripts go under `.wayfinder_runs/` and use `get_adapter()`:

### Fetch the vault “market” (single market list)

```python
"""Fetch Avantis avUSDC vault market stats (single-market adapter)."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.avantis_adapter import AvantisAdapter

async def main():
    adapter = get_adapter(AvantisAdapter)  # read-only, no wallet needed
    ok, markets = await adapter.get_all_markets()
    if not ok:
        raise RuntimeError(markets)
    m = markets[0]
    print(
        "vault=", m.get("vault"),
        "symbol=", m.get("symbol"),
        "tvl=", m.get("tvl"),
        "share_price=", m.get("share_price"),
        "total_supply=", m.get("total_supply"),
    )

if __name__ == "__main__":
    asyncio.run(main())
```

### Fetch vault manager state

```python
"""Fetch Avantis VaultManager state (balances/rewards/buffer ratio)."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.avantis_adapter import AvantisAdapter

async def main():
    adapter = get_adapter(AvantisAdapter)
    ok, state = await adapter.get_vault_manager_state()
    if not ok:
        raise RuntimeError(state)
    print(state)

if __name__ == "__main__":
    asyncio.run(main())
```

### Fetch a user position (shares, assets, maxRedeem/maxWithdraw)

```python
"""Fetch Avantis user position (ERC-4626 shares + assets)."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.avantis_adapter import AvantisAdapter
from wayfinder_paths.core.constants.contracts import AVANTIS_AVUSDC

USER = "0x0000000000000000000000000000000000000000"

async def main():
    adapter = get_adapter(AvantisAdapter)
    ok, pos = await adapter.get_pos(vault_address=AVANTIS_AVUSDC, account=USER, include_usd=False)
    if not ok:
        raise RuntimeError(pos)
    print(
        "shares=", pos.get("shares_balance"),
        "assets=", pos.get("assets_balance"),
        "max_redeem=", pos.get("max_redeem"),
        "max_withdraw=", pos.get("max_withdraw"),
    )

if __name__ == "__main__":
    asyncio.run(main())
```

## Key read methods

| Method | Purpose | Wallet needed? |
|--------|---------|----------------|
| `get_all_markets()` | Vault stats (single market) | No |
| `get_vault_manager_state(block_identifier?)` | VaultManager balances/rewards/buffer info | No |
| `get_pos(vault_address?, account?, include_usd?, block_identifier?)` | Shares/assets + maxRedeem/maxWithdraw | No (if you pass `account`) |
| `get_full_user_state(account, include_zero_positions?, include_usd?, block_identifier?)` | Normalized “positions” snapshot | No |

