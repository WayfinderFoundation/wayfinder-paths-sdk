# Pendle gotchas

## `fetch_markets()` vs `list_active_pt_yt_markets()` (IMPORTANT)

**`fetch_markets()`** returns raw API - data is **nested under `details`**:
```python
# WRONG - will be 0
implied = m.get("impliedApy")

# RIGHT - data is nested
implied = m.get("details", {}).get("impliedApy")
```

**`list_active_pt_yt_markets()`** returns **flattened** data:
```python
# This works - data is at top level
implied = m.get("fixedApy")  # (renamed from impliedApy)
```

**Rule:** Use `list_active_pt_yt_markets()` for market discovery. Only use `fetch_markets()` if you need raw API fields.

## Units (don't mix human vs raw)

- Hosted SDK expects `amountIn` in **raw base units** as a string
- Always resolve token decimals and convert explicitly

## Address formats

- Pendle APIs return IDs like `"42161-0xabc..."`
- `list_active_pt_yt_markets()` normalizes to plain `0x...` addresses
- `fetch_markets()` keeps the prefixed format

## Chain parameter

The adapter accepts both forms:
- `chain=42161` or `chain="arbitrum"`
- `chain=8453` or `chain="base"`

## "Fixed APY" naming

- `fixedApy` in `list_active_pt_yt_markets()` = `details.impliedApy` from raw API
- Treat as PT implied yield; actual execution can differ due to slippage

## Quote fields are optional

- Hosted SDK may omit `effectiveApy`/`impliedApy` depending on market state
- Always handle missing fields with `.get()` defaults

## Receiver vs signer mismatch

- `receiver` controls where output tokens go
- If `receiver != signer`, treat as high-risk and require explicit user confirmation
