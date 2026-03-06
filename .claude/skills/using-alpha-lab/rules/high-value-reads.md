# High-Value Reads

## Quick Reference

**User asks:** -> **Use this:**

- "What's today's alpha?" -> `search(created_after="2026-03-06T00:00:00Z")`
- "Best insights right now" -> `search()` (default: sorted by score desc)
- "Any good tweets?" -> `search(scan_type="twitter_post", min_score=0.5)`
- "Chain flow activity?" -> `search(scan_type="defi_llama_chain_flow")`
- "Top APY signals" -> `search(scan_type="delta_lab_top_apy")`
- "Search for ETH" -> `search(search="ETH")`
- "Latest insights" -> `search(sort="-created")`

## MCP Resources

```python
# Top insights (default sort by score desc)
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://alpha-lab/search/_/all/0/_/_/-insightfulness_score/20/0")

# Twitter posts only, min score 0.5
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://alpha-lab/search/_/twitter_post/0.5/_/_/-insightfulness_score/20/0")

# Text search for "ETH"
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://alpha-lab/search/ETH/all/0/_/_/-insightfulness_score/10/0")

# Recent first
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://alpha-lab/search/_/all/0/_/_/-created/20/0")

# List available types
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://alpha-lab/types")
```

**MCP URI format:** `wayfinder://alpha-lab/search/{search}/{scan_type}/{min_score}/{created_after}/{created_before}/{sort}/{limit}/{offset}`

- Use `_` for unused string params, `0` for unused numeric params, `all` for no type filter.

## Python Client

```python
from wayfinder_paths.core.clients import ALPHA_LAB_CLIENT

# Top 20 insights
data = await ALPHA_LAB_CLIENT.search(limit=20)

# High-score tweets from today
data = await ALPHA_LAB_CLIENT.search(
    scan_type="twitter_post",
    min_score=0.7,
    created_after="2026-03-06T00:00:00Z",
    sort="-insightfulness_score",
)

# Paginate through results
page1 = await ALPHA_LAB_CLIENT.search(limit=50, offset=0)
page2 = await ALPHA_LAB_CLIENT.search(limit=50, offset=50)
```
