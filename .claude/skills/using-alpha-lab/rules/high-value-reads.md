# High-Value Reads

## Quick Reference

**User asks:** -> **Use this:**

- "What's today's alpha?" -> `wayfinder://alpha-lab/search/_/all/2026-03-06T00:00:00Z/_/20`
- "Any good tweets?" -> `wayfinder://alpha-lab/search/_/twitter_post/_/_/20`
- "Chain flow activity?" -> `wayfinder://alpha-lab/search/_/defi_llama_chain_flow/_/_/20`
- "Top APY signals" -> `wayfinder://alpha-lab/search/_/delta_lab_top_apy/_/_/20`
- "Search for ETH" -> `wayfinder://alpha-lab/search/ETH/all/_/_/10`
- "Insights from last week" -> `wayfinder://alpha-lab/search/_/all/2026-02-27T00:00:00Z/_/20`

## MCP Resources

URI format: `wayfinder://alpha-lab/search/{query}/{scan_type}/{created_after}/{created_before}/{limit}`

| Param | Values | Default |
|-------|--------|---------|
| `query` | text search or `_` for none | `_` |
| `scan_type` | `all`, `twitter_post`, `defi_llama_chain_flow`, `defi_llama_overview`, `defi_llama_protocol`, `delta_lab_top_apy`, `delta_lab_best_delta_neutral` | `all` |
| `created_after` | ISO 8601 datetime or `_` to skip | `_` |
| `created_before` | ISO 8601 datetime or `_` to skip | `_` |
| `limit` | 1-200 | `20` |

Results are always sorted by insightfulness score (highest first).

```python
# Top 20 insights
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://alpha-lab/search/_/all/_/_/20")

# Twitter posts only
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://alpha-lab/search/_/twitter_post/_/_/10")

# Text search for "ETH"
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://alpha-lab/search/ETH/all/_/_/10")

# Today's insights
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://alpha-lab/search/_/all/2026-03-06T00:00:00Z/_/20")

# List available types
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://alpha-lab/types")
```

## Python Client (advanced)

Use the Python client when you need custom sort, min_score, or pagination:

```python
from wayfinder_paths.core.clients.AlphaLabClient import ALPHA_LAB_CLIENT

# Top 20 insights
data = await ALPHA_LAB_CLIENT.search(limit=20)

# High-score tweets from today
data = await ALPHA_LAB_CLIENT.search(
    scan_type="twitter_post",
    min_score=0.7,
    created_after="2026-03-06T00:00:00Z",
)

# Sort by newest first
data = await ALPHA_LAB_CLIENT.search(sort="-created", limit=20)

# Paginate through results
page1 = await ALPHA_LAB_CLIENT.search(limit=50, offset=0)
page2 = await ALPHA_LAB_CLIENT.search(limit=50, offset=50)
```
