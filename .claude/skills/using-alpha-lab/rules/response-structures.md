# Response Structures

## search() Response

```python
{
    "count": 142,        # Total matching results (not just this page)
    "results": [
        {
            "id": "abc-123",
            "type": "twitter_post",           # Scan type
            "insight": "ETH funding rates...", # The alpha content
            "insightfulness_score": 0.85,      # 0-1, higher = more notable
            "created": "2026-03-06T12:00:00Z", # When the insight was generated
            # ... additional type-specific fields
        },
        ...
    ]
}
```

### Key Fields

- `count` — Total matches across all pages (use with `offset` for pagination)
- `results` — List of insight objects for this page
- `type` — One of: `twitter_post`, `defi_llama_chain_flow`, `delta_lab_top_apy`, `delta_lab_best_delta_neutral`
- `insight` — The actual alpha content (text)
- `insightfulness_score` — 0-1 float; filter with `min_score`

## get_types() Response

Returns a list of available scan type strings:

```python
["twitter_post", "defi_llama_chain_flow", "delta_lab_top_apy", "delta_lab_best_delta_neutral"]
```
