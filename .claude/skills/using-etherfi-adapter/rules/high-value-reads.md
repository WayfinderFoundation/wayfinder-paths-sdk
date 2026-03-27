# ether.fi reads (positions + withdrawal status)

## Data accuracy (no guessing)

- Do **not** invent APYs, points, or rewards — this adapter does **not** provide them.
- Only report values returned from the adapter (raw ints from on-chain calls).
- If an RPC call fails, respond with "unavailable" and provide the exact script/call to reproduce.

## Primary data source

- Adapter: `wayfinder_paths/adapters/etherfi_adapter/adapter.py`
- Reads:
  - `get_pos(account?, chain_id?, block_identifier?, include_shares?)`
  - `is_withdraw_finalized(token_id, chain_id?, block_identifier?)`
  - `get_claimable_withdraw(token_id, chain_id?, block_identifier?)`

## Ad-hoc read scripts

All read scripts go under `.wayfinder_runs/` and use `get_adapter()`.

### Get eETH/weETH position (Ethereum mainnet)

```python
"""Read ether.fi position (eETH/weETH + conversions)."""
import asyncio

from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.etherfi_adapter import EtherfiAdapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_ETHEREUM

ACCOUNT = "0x0000000000000000000000000000000000000000"


async def main():
    adapter = await get_adapter(EtherfiAdapter)  # read-only, no wallet needed
    ok, pos = await adapter.get_pos(account=ACCOUNT, chain_id=CHAIN_ID_ETHEREUM)
    if not ok:
        raise RuntimeError(pos)

    print("eETH balance_raw:", pos["eeth"]["balance_raw"])
    print("weETH balance_raw:", pos["weeth"]["balance_raw"])
    print("weETH eETH_equivalent_raw:", pos["weeth"]["eeth_equivalent_raw"])
    print("weETH rate:", pos["weeth"]["rate"])


if __name__ == "__main__":
    asyncio.run(main())
```

### Check a withdraw request (tokenId)

```python
"""Check whether a WithdrawRequest NFT is finalized and how much is claimable."""
import asyncio

from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.etherfi_adapter import EtherfiAdapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_ETHEREUM

TOKEN_ID = 0


async def main():
    adapter = await get_adapter(EtherfiAdapter)

    ok, finalized = await adapter.is_withdraw_finalized(
        token_id=TOKEN_ID, chain_id=CHAIN_ID_ETHEREUM
    )
    if not ok:
        raise RuntimeError(finalized)

    ok, claimable = await adapter.get_claimable_withdraw(
        token_id=TOKEN_ID, chain_id=CHAIN_ID_ETHEREUM
    )
    if not ok:
        raise RuntimeError(claimable)

    print("finalized:", finalized)
    print("claimable_wei:", claimable)


if __name__ == "__main__":
    asyncio.run(main())
```

## Key read methods

| Method | Purpose | Wallet needed? |
|--------|---------|----------------|
| `get_pos(account?, chain_id?, block_identifier?, include_shares?)` | eETH/weETH balances + weETH rate + weETH→eETH equivalent + pooled ETH | No (if you pass `account`) |
| `is_withdraw_finalized(token_id, chain_id?, block_identifier?)` | Check whether a withdraw request NFT can be claimed | No |
| `get_claimable_withdraw(token_id, chain_id?, block_identifier?)` | Claimable ETH amount (wei); returns `0` if not finalized | No |
