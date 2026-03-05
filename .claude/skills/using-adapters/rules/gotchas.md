# Adapter gotchas (cross-cutting)

## Tuple-return convention

- Adapter methods return `(ok, data)` tuples.
- Always destructure and handle the error path:

```python
ok, data = await adapter.some_method(...)
if not ok:
    raise RuntimeError(data)
```

## Chain IDs + units

- Some adapters accept `chain_id`; others are mainnet-only and hard-code chain selection.
- Amounts are usually raw integers (token units). Convert from human units using token decimals.

## Reuse existing helpers

- Prefer repo helpers (e.g. `get_adapter()`, token metadata utilities, `TokenClient`) over new bespoke helpers.

