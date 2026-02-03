# BRAP Adapter

Adapter for cross-chain swaps and bridges via the BRAP (Bridge/Router/Adapter Protocol).

- **Type**: `BRAP`
- **Module**: `wayfinder_paths.adapters.brap_adapter.adapter.BRAPAdapter`

## Usage

```python
from wayfinder_paths.adapters.brap_adapter.adapter import BRAPAdapter

adapter = BRAPAdapter(strategy_wallet_signing_callback=signing_callback)
```

## Methods

### best_quote

Get the best quote for a swap.

```python
success, quote = await adapter.best_quote(
    from_token_address="0x...",
    to_token_address="0x...",
    from_chain_id=8453,
    to_chain_id=1,
    from_address="0x...",
    amount="1000000000",
    preferred_providers=["enso"],  # optional
)
if success:
    print(f"Output: {quote.get('output_amount')}")
```

### swap_from_token_ids

Execute a swap using token IDs.

```python
success, result = await adapter.swap_from_token_ids(
    from_token_id="usd-coin-base",
    to_token_id="ethereum",
    from_address="0x...",
    amount="1000000000",
    strategy_name="my_strategy",
    preferred_providers=["enso"],
)
if success:
    print(f"TX: {result.get('tx_hash')}")
```

### swap_from_quote

Execute a swap from a pre-fetched quote.

```python
success, result = await adapter.swap_from_quote(
    from_token=from_token_info,
    to_token=to_token_info,
    from_address="0x...",
    quote=quote,
    strategy_name="my_strategy",
)
```

## Testing

```bash
poetry run pytest wayfinder_paths/adapters/brap_adapter/ -v
```
