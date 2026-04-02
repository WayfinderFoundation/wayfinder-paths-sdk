# EigenCloud reads (restaking + delegation + queued withdrawals)

## Data accuracy (no guessing)

- Do **not** invent share prices, queued-withdrawal state, or claim eligibility.
- Read strategy, delegation, and rewards state from the adapter.
- If a call fails, respond with "unavailable" and include the exact adapter call.

## Primary data source

- Adapter: `wayfinder_paths/adapters/eigencloud_adapter/adapter.py`
- README: `wayfinder_paths/adapters/eigencloud_adapter/README.md`
- Chain: Ethereum mainnet only

## High-value reads

### Strategy / market list

- `ok, markets = await adapter.get_all_markets(include_total_shares=True, include_share_to_underlying=True)`
- Output rows include:
  - `strategy`, `strategy_name`
  - `underlying`, `underlying_symbol`, `underlying_decimals`
  - `is_whitelisted_for_deposit`
  - optional `total_shares`
  - optional `shares_to_underlying_1e18`

Use this as the canonical strategy discovery surface.

### Delegation state

- `ok, state = await adapter.get_delegation_state(account="0x...")`
- Use this before recommending delegate / undelegate / redelegate actions.

### Position snapshot

- `ok, pos = await adapter.get_pos(account="0x...", include_usd=False)`
- Output includes:
  - `account`, `isDelegated`, `delegatedTo`
  - `positions`: deposited and withdrawable shares by strategy
  - optional `usd_value`

### Queued withdrawals and full state

- `ok, state = await adapter.get_full_user_state(account="0x...", include_queued_withdrawals=True, withdrawal_roots=[...], include_rewards_metadata=True)`

Important:
- `get_full_user_state(...)` only includes queued withdrawals if you supply `withdrawal_roots`.
- The adapter can read a single queued withdrawal directly with `get_queued_withdrawal(withdrawal_root=...)`.
- To extract withdrawal roots from a queue transaction, use `get_withdrawal_roots_from_tx_hash(tx_hash=...)`.

### Rewards metadata

- `ok, metadata = await adapter.get_rewards_metadata(account="0x...")`
- `ok, check = await adapter.check_claim(claim=...)`

Use `get_rewards_metadata(...)` to inspect the current distribution root and claimer state. Use `check_claim(...)` before broadcasting a rewards claim if you already have a claim struct.

## Ad-hoc read script

```python
"""List EigenCloud strategies and a wallet position snapshot."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.eigencloud_adapter import EigenCloudAdapter

ACCOUNT = "0x0000000000000000000000000000000000000000"

async def main() -> None:
    adapter = await get_adapter(EigenCloudAdapter)
    ok, markets = await adapter.get_all_markets()
    if not ok:
        raise RuntimeError(markets)
    print("strategies=", len(markets))

    ok, pos = await adapter.get_pos(account=ACCOUNT, include_usd=False)
    if not ok:
        raise RuntimeError(pos)
    print(pos["delegatedTo"], len(pos["positions"]))

if __name__ == "__main__":
    asyncio.run(main())
```

## Method summary

| Method | Returns | Best for |
|--------|---------|----------|
| `get_all_markets(...)` | Strategy list | Strategy discovery |
| `get_delegation_state(...)` | Delegation snapshot | Operator state |
| `get_pos(...)` | Position dict | Restaked balances |
| `get_full_user_state(...)` | Aggregated account state | Full wallet reporting |
| `get_withdrawal_roots_from_tx_hash(...)` | `list[str]` | Queue-follow-up workflows |
| `get_queued_withdrawal(...)` | One queued withdrawal | Completion readiness |
| `get_rewards_metadata(...)` | Rewards metadata | Claim preparation |
| `check_claim(...)` | Claim preflight | Reward validation |
