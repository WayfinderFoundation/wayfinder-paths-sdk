# Hyperlend execution opportunities (writes)

## Primary execution surface

- Adapter: `wayfinder_paths/adapters/hyperlend_adapter/adapter.py`

### Supply (lend)

- Call: `HyperlendAdapter.lend(underlying_token, qty, chain_id, native=False)`
- Inputs:
  - `qty` is an **int in raw base units** (wei for ERC20; for native token, raw wei)
  - `underlying_token` is a token contract address (checksum-able)
- Behavior:
  - For ERC20: may submit an ERC20 approval tx before supplying.
  - Broadcasts the supply tx.

### Withdraw (unlend)

- Call: `HyperlendAdapter.unlend(underlying_token, qty, chain_id, native=False)`
- Same unit rules as `lend`.

## Best practice: keep conversion explicit

1) Resolve token decimals (TokenClient) for the underlying
2) Convert human → raw
3) Call `lend/unlend` with raw units

