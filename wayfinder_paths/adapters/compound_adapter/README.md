# CompoundAdapter

Adapter for Compound III / Comet multi-market lending and rewards.

- **Type**: `COMPOUND`
- **Module**: `wayfinder_paths.adapters.compound_adapter.adapter.CompoundAdapter`

## Overview

- Targets the Comet proxy surface, not Compound v2.
- Supports base supply/withdraw, base borrow/repay, collateral supply/withdraw, and reward claims.
- Discovers base/collateral/reward metadata on-chain from the configured official market registry.

## Supported Networks

Currently configured in this repo:

- Ethereum (`1`)
- Base (`8453`)
- Arbitrum (`42161`)
- Polygon (`137`)

No longer configured / not supported by this adapter in this repo:

- Optimism (`10`)
- Scroll (`534352`)
- Linea (`59144`)
- Ronin (`2020`)
- Unichain (`130`)
- Mantle (`5000`)

## Usage

```python
from wayfinder_paths.adapters.compound_adapter import CompoundAdapter

adapter = CompoundAdapter(
    config={},
    sign_callback=sign_callback,
    wallet_address="0x...",
)
```

## Testing

```bash
poetry run pytest wayfinder_paths/adapters/compound_adapter/ -v
```
