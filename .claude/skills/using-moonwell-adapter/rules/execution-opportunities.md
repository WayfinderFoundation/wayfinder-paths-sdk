# Moonwell Execution

## Scope

Use `MoonwellAdapter` for Moonwell Core mToken flows:

- Supply underlying with `lend()`
- Redeem mTokens with `unlend()`
- Borrow and repay underlying with `borrow()` / `repay()`
- Enable or disable collateral with `set_collateral()` / `remove_collateral()`
- Claim rewards with `claim_rewards()` on Base and OP Mainnet
- Wrap native gas token with `wrap_eth()` for the chain's wrapped native token

Use `MorphoAdapter` for Moonwell Morpho Vaults and isolated markets.

## Safety Checklist

Before a write:

- Confirm the target `chain_id`.
- Confirm the mToken address belongs to that chain.
- Confirm the underlying token address and raw integer amount.
- Check gas on the target chain.
- For borrowing, call `get_borrowable_amount()` first.
- For withdrawing, call `max_withdrawable_mtoken()` and pass `cTokens_raw` to `unlend()`.
- Run a Gorlami fork simulation for non-trivial EVM flows when available.

## Supply

```python
import asyncio

from wayfinder_paths.adapters.moonwell_adapter import MoonwellAdapter
from wayfinder_paths.mcp.scripting import get_adapter

CHAIN_ID = 8453
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
M_USDC = "0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22"


async def main():
    adapter = await get_adapter(MoonwellAdapter, "main")
    ok, tx = await adapter.lend(
        chain_id=CHAIN_ID,
        mtoken=M_USDC,
        underlying_token=USDC,
        amount=100 * 10**6,
    )
    print(ok, tx)


if __name__ == "__main__":
    asyncio.run(main())
```

`lend()` approves the mToken spender before calling `mint(amount)`.

## Borrow And Repay

```python
import asyncio

from wayfinder_paths.adapters.moonwell_adapter import MoonwellAdapter
from wayfinder_paths.mcp.scripting import get_adapter

CHAIN_ID = 8453
WETH = "0x4200000000000000000000000000000000000006"
M_WETH = "0x628ff693426583D9a7FB391E54366292F509D457"


async def main():
    adapter = await get_adapter(MoonwellAdapter, "main")
    ok, liquidity = await adapter.get_borrowable_amount(chain_id=CHAIN_ID)
    if not ok or liquidity <= 0:
        raise RuntimeError(f"Not borrowable: {liquidity}")

    ok, tx = await adapter.borrow(chain_id=CHAIN_ID, mtoken=M_WETH, amount=10**16)
    print("borrow", ok, tx)

    ok, tx = await adapter.repay(
        chain_id=CHAIN_ID,
        mtoken=M_WETH,
        underlying_token=WETH,
        amount=10**16,
        repay_full=False,
    )
    print("repay", ok, tx)


if __name__ == "__main__":
    asyncio.run(main())
```

`repay()` approves the mToken spender before calling `repayBorrow()`. With
`repay_full=True`, it passes `MAX_UINT256` to `repayBorrow`.

## Collateral

```python
ok, tx = await adapter.set_collateral(chain_id=8453, mtoken=M_USDC)
ok, entered = await adapter.is_market_entered(chain_id=8453, mtoken=M_USDC)
ok, tx = await adapter.remove_collateral(chain_id=8453, mtoken=M_USDC)
```

Supplying does not enable collateral automatically. Do not borrow until the
collateral entry check returns true.

## Withdraw

```python
ok, info = await adapter.max_withdrawable_mtoken(chain_id=8453, mtoken=M_USDC)
if ok and info["cTokens_raw"] > 0:
    ok, tx = await adapter.unlend(
        chain_id=8453,
        mtoken=M_USDC,
        amount=info["cTokens_raw"],
    )
```

`unlend()` calls `redeem()` and expects mToken units, not underlying units.

## Rewards

```python
ok, rewards = await adapter.claim_rewards(chain_id=8453, min_rewards_usd=1.0)
```

Base and OP Mainnet have Multi-Reward Distributor addresses. Moonbeam and Moonriver
do not, so reward claim calls return an empty dict and do not broadcast.

## Native Wrapping

```python
ok, tx = await adapter.wrap_eth(chain_id=8453, amount=10**16)
```

The method name is kept for compatibility, but it targets the configured wrapped
native token for the selected network: `WETH`, `WGLMR`, or `WMOVR`.
