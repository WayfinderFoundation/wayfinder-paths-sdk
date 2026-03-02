# Euler v2 execution (deposit/withdraw/borrow/repay via EVC batch)

## Safety

- Prefer running the existing fork simulation first:
  - `poetry run pytest wayfinder_paths/adapters/euler_v2_adapter/test_gorlami_simulation.py -v`
  - Note: this test requires the gorlami fork proxy to be configured and reachable (see `TESTING.md`).
- For real transactions, use MCP `run_script(...)` so the safety review hook can show a preview (Euler adapter flows run inside a Python script; `execute(...)` doesn’t cover them).

## Execution wiring (strategy wallet + signing callback)

`EulerV2Adapter` expects:
- `config["strategy_wallet"]["address"]` to be set
- `strategy_wallet_signing_callback` to be passed to the constructor

In ad-hoc scripts under `.wayfinder_runs/`, wire it like this:

```python
"""Create a write-enabled EulerV2Adapter from a local wallet label."""
from wayfinder_paths.mcp.scripting import get_adapter, _resolve_wallet
from wayfinder_paths.adapters.euler_v2_adapter import EulerV2Adapter

sign_cb, addr = _resolve_wallet("main")
adapter = get_adapter(
    EulerV2Adapter,
    config_overrides={"strategy_wallet": {"address": addr}},
    strategy_wallet_signing_callback=sign_cb,
)
```

## Common flows (adapter methods)

### Deposit + enable collateral

```python
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter, _resolve_wallet
from wayfinder_paths.adapters.euler_v2_adapter import EulerV2Adapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE

COLLATERAL_VAULT = "0x0000000000000000000000000000000000000000"
AMOUNT = 1_000_000  # raw underlying units (example: 1 USDC if 6 decimals)

async def main():
    sign_cb, addr = _resolve_wallet("main")
    adapter = get_adapter(
        EulerV2Adapter,
        config_overrides={"strategy_wallet": {"address": addr}},
        strategy_wallet_signing_callback=sign_cb,
    )

    ok, tx = await adapter.lend(chain_id=CHAIN_ID_BASE, vault=COLLATERAL_VAULT, amount=AMOUNT)
    if not ok:
        raise RuntimeError(tx)

    ok, tx = await adapter.set_collateral(chain_id=CHAIN_ID_BASE, vault=COLLATERAL_VAULT, use_as_collateral=True)
    if not ok:
        raise RuntimeError(tx)

    print("tx=", tx)

if __name__ == "__main__":
    asyncio.run(main())
```

### Borrow (optionally enabling collateral/controller in the same tx)

```python
ok, tx = await adapter.borrow(
    chain_id=CHAIN_ID_BASE,
    vault="0x...",          # the borrow vault
    amount=123,             # raw underlying units
    collateral_vaults=[COLLATERAL_VAULT],  # optional convenience
    enable_controller=True,               # default True
)
```

### Repay full + withdraw full

```python
ok, tx = await adapter.repay(chain_id=CHAIN_ID_BASE, vault="0x...", amount=0, repay_full=True)
ok, tx = await adapter.remove_collateral(chain_id=CHAIN_ID_BASE, vault=COLLATERAL_VAULT)
ok, tx = await adapter.unlend(chain_id=CHAIN_ID_BASE, vault=COLLATERAL_VAULT, amount=0, withdraw_full=True)
```

## Key execution methods

| Method | Purpose | Notes |
|--------|---------|-------|
| `lend(chain_id, vault, amount, receiver?)` | Deposit underlying to a vault | May send an ERC20 approval tx first; deposit itself is a single EVC batch tx |
| `unlend(chain_id, vault, amount, receiver?, withdraw_full?)` | Withdraw underlying from a vault | `withdraw_full=True` redeems **all shares** |
| `set_collateral(chain_id, vault, use_as_collateral?, account?)` | Enable/disable collateral | Uses EVC `enableCollateral/disableCollateral` |
| `borrow(chain_id, vault, amount, receiver?, collateral_vaults?, enable_controller?)` | Borrow underlying from a vault | Can batch-enable collateral + controller before borrow |
| `repay(chain_id, vault, amount, receiver?, repay_full?)` | Repay borrow | May send an ERC20 approval tx first; `repay_full=True` uses `MAX_UINT256` semantics |
