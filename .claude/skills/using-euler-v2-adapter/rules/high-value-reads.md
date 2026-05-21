# Euler v2 reads (vaults + positions)

## Data accuracy (no guessing)

- Do **not** invent or estimate APYs, caps, cash, totals, or LTVs.
- Only report values fetched from Euler contracts via the adapter.
- If an RPC call fails, respond with "unavailable" and provide the exact script/call to reproduce.

## Primary data source

- Adapter: `wayfinder_paths/adapters/euler_v2_adapter/adapter.py`
- Deployments/perspectives: `wayfinder_paths/core/constants/euler_v2_contracts.py`
- Current curated vault labels: Euler `euler-labels` repository via
  `adapter.get_labelled_vaults(...)`
- Indexed API reads: Euler V3 API preview via `adapter.get_indexed_vaults(...)`,
  `adapter.get_euler_earn_vaults(...)`, `adapter.resolve_vault(...)`, and
  `adapter.get_offchain_prices(...)`

Terminology:
- **Vault** = market address and also the ERC-4626 share token.
- **Underlying** = `vault.asset()`
- **Debt token** = `vault.dToken()`
- **EulerEarn vault** = ERC-4626 meta-vault that allocates into EVK/other
  strategies; not a borrowable EVK liability vault.
- **EulerSwap** = swap/multiply periphery surface. This adapter exposes current
  contract addresses but does not build or execute order-flow payloads.

## Source selection

- For current curated EVK/Earn discovery, prefer
  `get_labelled_vaults(chain_id=...)`.
- For indexed summaries, vault details, collateral rows, totals/history, prices,
  or Earn summaries, use the Euler V3 API preview methods.
- For exact current on-chain EVK state, use lens-backed methods:
  `get_vault_info_full(...)`, `get_all_markets(...)`, and
  `get_full_user_state(...)`.
- `get_verified_vaults(...)` still reads Perspective `verifiedArray()` for
  on-chain compatibility. Euler docs mark governed Perspective discovery as
  deprecated for verified metadata, so do not treat it as the only current source
  of truth.

V3 API conventions:
- Raw on-chain amounts are strings to preserve bigint precision. The adapter
  preserves these original fields and mirrors integer strings to `*_raw` Python
  int fields when it can do so exactly.
- APYs from V3 API endpoints are percent values (`5.25` means 5.25%). The
  adapter preserves the original percent fields and mirrors them to
  `*_decimal` fields (`0.0525`) for consistency with strategy math.
- Lens-backed `get_all_markets(...)` converts ray APYs to decimal fractions
  (`0.0525` means 5.25%).

## Ad-hoc read scripts

All read scripts go under `.wayfinder_runs/` and use `get_adapter()`:

### Current curated EVK/Earn vaults (Euler labels)

```python
"""Fetch current Euler labels for curated EVK and Earn vaults."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.euler_v2_adapter import EulerV2Adapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE

async def main():
    adapter = await get_adapter(EulerV2Adapter)
    ok, labels = await adapter.get_labelled_vaults(chain_id=CHAIN_ID_BASE)
    if not ok:
        raise RuntimeError(labels)
    print("evk_vaults=", labels["evk_vaults"][:20])
    print("earn_vaults=", labels["earn_vaults"][:20])

if __name__ == "__main__":
    asyncio.run(main())
```

### Indexed EVK vault summaries (Euler V3 API)

```python
"""Fetch indexed Euler EVK vault summaries."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.euler_v2_adapter import EulerV2Adapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE

async def main():
    adapter = await get_adapter(EulerV2Adapter)
    ok, res = await adapter.get_indexed_vaults(
        chain_id=CHAIN_ID_BASE,
        limit=20,
        fields=["chainId", "address", "name", "symbol", "asset", "supplyApy", "borrowApy", "totalAssets"],
    )
    if not ok:
        raise RuntimeError(res)
    for v in res["data"] or []:
        print(v["symbol"], v["address"], "supplyApyPct=", v.get("supplyApy"))

if __name__ == "__main__":
    asyncio.run(main())
```

### One EVK vault detail, collateral rows, and indexed totals

```python
"""Fetch indexed Euler EVK vault detail and current/historical totals."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.euler_v2_adapter import EulerV2Adapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE

VAULT = "0x0000000000000000000000000000000000000000"

async def main():
    adapter = await get_adapter(EulerV2Adapter)

    ok, detail = await adapter.get_indexed_vault(chain_id=CHAIN_ID_BASE, vault=VAULT)
    if not ok:
        raise RuntimeError(detail)
    print(detail["data"]["symbol"], detail["data"].get("supply_apy_decimal"))

    ok, collaterals = await adapter.get_indexed_vault_collaterals(
        chain_id=CHAIN_ID_BASE,
        vault=VAULT,
    )
    if not ok:
        raise RuntimeError(collaterals)
    print("collaterals=", collaterals["data"])

    ok, totals = await adapter.get_indexed_vault_totals(chain_id=CHAIN_ID_BASE, vault=VAULT)
    if not ok:
        raise RuntimeError(totals)
    print("current=", totals["data"].get("current"))

if __name__ == "__main__":
    asyncio.run(main())
```

### Indexed EulerEarn vault summaries

```python
"""Fetch indexed EulerEarn vault summaries."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.euler_v2_adapter import EulerV2Adapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE

async def main():
    adapter = await get_adapter(EulerV2Adapter)
    ok, res = await adapter.get_euler_earn_vaults(chain_id=CHAIN_ID_BASE, limit=20)
    if not ok:
        raise RuntimeError(res)
    for v in res["data"] or []:
        print(v["symbol"], v["address"], "apy30dPct=", v.get("apy30d"))

if __name__ == "__main__":
    asyncio.run(main())
```

Use `get_euler_earn_vault(chain_id=..., vault=...)` for one indexed Earn vault
detail. This is still read-only discovery; the adapter does not expose Earn
deposit or withdraw methods.

### Off-chain prices (Euler V3 API)

```python
"""Fetch current Euler off-chain USD prices for tokens."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.euler_v2_adapter import EulerV2Adapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

async def main():
    adapter = await get_adapter(EulerV2Adapter)
    ok, res = await adapter.get_offchain_prices(chain_id=CHAIN_ID_BASE, addresses=[USDC])
    if not ok:
        raise RuntimeError(res)
    print(res["data"])

if __name__ == "__main__":
    asyncio.run(main())
```

### List verified vaults (deprecated Perspective compatibility)

```python
"""List Euler v2 verified vaults for a chain/perspective."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.euler_v2_adapter import EulerV2Adapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE

async def main():
    adapter = await get_adapter(EulerV2Adapter)  # read-only, no wallet needed
    ok, vaults = await adapter.get_verified_vaults(chain_id=CHAIN_ID_BASE, perspective="governed", limit=50)
    if not ok:
        raise RuntimeError(vaults)
    for v in vaults:
        print(v)

if __name__ == "__main__":
    asyncio.run(main())
```

### Fetch markets (vault list + APYs + caps + LTV rows)

```python
"""Fetch Euler v2 markets for a chain."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.euler_v2_adapter import EulerV2Adapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE

async def main():
    adapter = await get_adapter(EulerV2Adapter)  # read-only, no wallet needed
    ok, markets = await adapter.get_all_markets(chain_id=CHAIN_ID_BASE, perspective="governed", limit=60, concurrency=10)
    if not ok:
        raise RuntimeError(markets)
    for m in markets:
        print(
            m.get("asset_symbol"),
            "vault=", m.get("vault"),
            "supply_apy=", m.get("supply_apy"),
            "borrow_apy=", m.get("borrow_apy"),
            "cash=", m.get("cash"),
        )

if __name__ == "__main__":
    asyncio.run(main())
```

### Fetch a single vault’s full info (raw lens output)

```python
"""Fetch Euler v2 vault info from VaultLens (raw-ish, but structured)."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.euler_v2_adapter import EulerV2Adapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE

VAULT = "0x0000000000000000000000000000000000000000"

async def main():
    adapter = await get_adapter(EulerV2Adapter)
    ok, info = await adapter.get_vault_info_full(chain_id=CHAIN_ID_BASE, vault=VAULT)
    if not ok:
        raise RuntimeError(info)
    print("asset=", info.get("asset"), "symbol=", info.get("assetSymbol"), "supplyCap=", info.get("supplyCap"))

if __name__ == "__main__":
    asyncio.run(main())
```

### Fetch a user snapshot (enabled vaults + balances)

```python
"""Fetch Euler v2 user snapshot for a chain."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.euler_v2_adapter import EulerV2Adapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE

USER = "0x0000000000000000000000000000000000000000"

async def main():
    adapter = await get_adapter(EulerV2Adapter)  # read-only, no wallet needed
    ok, state = await adapter.get_full_user_state(chain_id=CHAIN_ID_BASE, account=USER, include_zero_positions=False)
    if not ok:
        raise RuntimeError(state)
    for p in state.get("positions", []):
        if int(p.get("assets") or 0) or int(p.get("borrowed") or 0):
            print(
                "vault=", p.get("vault"),
                "assets=", p.get("assets"),
                "borrowed=", p.get("borrowed"),
                "is_collateral=", p.get("is_collateral"),
                "is_controller=", p.get("is_controller"),
            )

if __name__ == "__main__":
    asyncio.run(main())
```

## Key read methods

| Method | Purpose | Wallet needed? |
|--------|---------|----------------|
| `get_labelled_vaults(chain_id, include_products?, include_earn?)` | Current curated EVK/Earn vault addresses from Euler labels | No |
| `get_indexed_vaults(chain_id, limit?, offset?, fields?, sort?, asset?, min_tvl?, max_tvl?, visibility?)` | Euler V3 indexed EVK vault summaries | No |
| `get_indexed_vault(chain_id, vault)` | Euler V3 indexed EVK vault detail | No |
| `get_indexed_vault_collaterals(chain_id, vault, limit?, offset?)` | Euler V3 indexed EVK collateral/LTV rows | No |
| `get_indexed_vault_totals(chain_id, vault)` | Euler V3 indexed EVK current totals and history | No |
| `get_euler_earn_vaults(chain_id, limit?, offset?)` | Euler V3 indexed EulerEarn vault summaries | No |
| `get_euler_earn_vault(chain_id, vault)` | Euler V3 indexed EulerEarn vault detail | No |
| `resolve_vault(chain_id, address)` | Resolve whether an address is an indexed EVK/Earn vault | No |
| `get_offchain_prices(chain_id, addresses)` | Euler V3 token USD prices | No |
| `get_protocol_contracts(chain_id)` | Current EVC/EVK/Earn/Swap/lens/periphery addresses | No |
| `get_verified_vaults(chain_id, perspective?, limit?)` | Perspective `verifiedArray()` vault addresses; deprecated as sole verified metadata source | No |
| `get_all_markets(chain_id, perspective?, limit?, concurrency?)` | Vault list + supply/borrow APYs + caps + LTV rows | No |
| `get_vault_info_full(chain_id, vault)` | VaultLens `getVaultInfoFull` output | No |
| `get_full_user_state(chain_id, account, include_zero_positions?)` | Enabled vaults + balances + flags for one chain | No (if you pass `account`) |
