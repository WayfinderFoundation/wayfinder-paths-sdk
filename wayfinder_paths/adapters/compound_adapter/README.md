# CompoundAdapter

Adapter for Compound III / Comet multi-market lending and rewards.

- **Type**: `COMPOUND`
- **Module**: `wayfinder_paths.adapters.compound_adapter.adapter.CompoundAdapter`

## Overview

- Targets the Comet proxy surface, not Compound v2.
- Supports base supply/withdraw, base borrow/repay, collateral supply/withdraw, and reward claims.
- Discovers base/collateral/reward metadata on-chain from the configured official market registry.

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
