---
name: using-alpha-lab
description: How to use the Alpha Lab client in Wayfinder Paths for discovering alpha insights — scored tweets, chain flows, top APYs, and delta-neutral opportunities.
metadata:
  tags: wayfinder, alpha-lab, alpha, insights, twitter, chain-flows
---

## What you need to know (TL;DR)

**Alpha Lab = Scored alpha insight feed**

```python
from wayfinder_paths.core.clients import ALPHA_LAB_CLIENT

# Search all insights (sorted by score)
await ALPHA_LAB_CLIENT.search()

# Filter by type
await ALPHA_LAB_CLIENT.search(scan_type="twitter_post", min_score=0.7)

# Text search
await ALPHA_LAB_CLIENT.search(search="ETH funding", limit=10)

# List available scan types
await ALPHA_LAB_CLIENT.get_types()
```

**Critical gotchas:**
- Client returns data directly (not tuples) — `data = await ALPHA_LAB_CLIENT.search()`
- Scores are 0-1 floats (1 = most insightful)
- `sort` prefix `-` means descending: `"-insightfulness_score"` (default, highest first)
- Max 200 results per call; use `offset` for pagination

## When to use

- "What's the latest alpha?" / "Show me today's insights"
- "Any interesting tweets about ETH?"
- "What chain flows are happening?"
- "What are the highest-scored insights?"

## How to use

- [rules/what-is-alpha-lab.md](rules/what-is-alpha-lab.md) - What Alpha Lab is and its scan types
- [rules/high-value-reads.md](rules/high-value-reads.md) - Core queries and MCP URIs
- [rules/response-structures.md](rules/response-structures.md) - Response shapes
- [rules/gotchas.md](rules/gotchas.md) - Common mistakes
