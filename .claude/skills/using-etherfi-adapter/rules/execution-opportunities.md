# ether.fi execution (stake, wrap/unwrap, async withdrawals)

## Safety

- Prefer running fork simulations first:
  - `poetry run pytest wayfinder_paths/adapters/etherfi_adapter/test_gorlami_simulation.py -v`
- For real transactions, write a script under `.wayfinder_runs/` and run it via `mcp__wayfinder__run_script` (which triggers the safety review hook).

## Common flows (adapter methods)

### Stake ETH -> eETH

```python
"""Stake 1 ETH into ether.fi (ETH -> eETH)."""
import asyncio

from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.etherfi_adapter import EtherfiAdapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_ETHEREUM


async def main():
    adapter = get_adapter(EtherfiAdapter, "main")  # wallet required for signing

    ok, tx = await adapter.stake_eth(amount_wei=10**18, chain_id=CHAIN_ID_ETHEREUM)
    if not ok:
        raise RuntimeError(tx)
    print("tx:", tx)


if __name__ == "__main__":
    asyncio.run(main())
```

### Wrap eETH -> weETH (approval + wrap)

```python
"""Wrap full eETH balance into weETH (uses ERC20 approve then wrap)."""
import asyncio

from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.etherfi_adapter import EtherfiAdapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_ETHEREUM


async def main():
    adapter = get_adapter(EtherfiAdapter, "main")

    ok, pos = await adapter.get_pos(chain_id=CHAIN_ID_ETHEREUM)
    if not ok:
        raise RuntimeError(pos)

    amount_eeth = int(pos["eeth"]["balance_raw"])
    ok, tx = await adapter.wrap_eeth(amount_eeth=amount_eeth, chain_id=CHAIN_ID_ETHEREUM)
    if not ok:
        raise RuntimeError(tx)
    print("tx:", tx)


if __name__ == "__main__":
    asyncio.run(main())
```

### Wrap eETH -> weETH (single tx with permit)

- Call: `wrap_eeth_with_permit(amount_eeth, permit, chain_id=...)`
- `permit` must be a dict (or 5-tuple/list) shaped like:
  - `{"value": int, "deadline": int, "v": int, "r": bytes|hex, "s": bytes|hex}`

If you need a reference for building an EIP-2612 permit for eETH, see:
- `wayfinder_paths/adapters/etherfi_adapter/test_gorlami_simulation.py`

### Unwrap weETH -> eETH

```python
ok, tx = await adapter.unwrap_weeth(amount_weeth=..., chain_id=CHAIN_ID_ETHEREUM)
```

### Request an async withdrawal (mints a WithdrawRequest NFT)

```python
ok, res = await adapter.request_withdraw(amount_eeth=..., chain_id=CHAIN_ID_ETHEREUM, include_request_id=True)
request_id = res["request_id"]  # WithdrawRequest NFT tokenId (may be None if parsing fails)
```

### Request an async withdrawal (single tx with permit)

```python
ok, res = await adapter.request_withdraw_with_permit(amount_eeth=..., permit=..., chain_id=CHAIN_ID_ETHEREUM, include_request_id=True)
request_id = res["request_id"]
```

### Claim a finalized withdrawal (burns NFT -> receive ETH)

```python
# Recommended: check finalized/claimable before trying to claim.
ok, finalized = await adapter.is_withdraw_finalized(token_id=request_id, chain_id=CHAIN_ID_ETHEREUM)
ok, tx = await adapter.claim_withdraw(token_id=request_id, chain_id=CHAIN_ID_ETHEREUM)
```

## Key execution methods

Returns: `request_withdraw*` return a dict (`{"tx", "recipient"/"owner", "amount_eeth", "request_id"}`). All other methods return a tx hash string.

| Method | Purpose | Notes |
|--------|---------|-------|
| `stake_eth(amount_wei, chain_id=CHAIN_ID_ETHEREUM, check_paused=True)` | Stake ETH → receive eETH shares | Payable tx; fails fast if pool paused when `check_paused=True` |
| `wrap_eeth(amount_eeth, chain_id=CHAIN_ID_ETHEREUM, approval_amount=MAX_UINT256)` | Wrap eETH → weETH | Sends an ERC20 approval tx first unless already approved |
| `wrap_eeth_with_permit(amount_eeth, permit, chain_id=CHAIN_ID_ETHEREUM)` | Wrap eETH → weETH (single tx) | Skips approval; `permit` must be EIP-2612-shaped |
| `unwrap_weeth(amount_weeth, chain_id=CHAIN_ID_ETHEREUM)` | Unwrap weETH → eETH | No approvals required |
| `request_withdraw(amount_eeth, recipient=None, chain_id=CHAIN_ID_ETHEREUM, approval_amount=MAX_UINT256, include_request_id=True)` | Start async withdrawal; mints WithdrawRequest NFT | Returns dict; `request_id` may be `None` even if tx succeeded |
| `request_withdraw_with_permit(owner=None, amount_eeth, permit, chain_id=CHAIN_ID_ETHEREUM, include_request_id=True)` | Start async withdrawal (single tx) | Returns dict; NFT minted to `owner`; `permit` must be EIP-2612-shaped |
| `claim_withdraw(token_id, chain_id=CHAIN_ID_ETHEREUM)` | Claim finalized withdrawal; burn NFT → receive ETH | Must be called by current NFT owner; fails if not finalized |
