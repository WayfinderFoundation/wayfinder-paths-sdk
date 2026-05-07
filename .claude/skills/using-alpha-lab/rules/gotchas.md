# Gotchas

## Quick Cheat Sheet

| Wrong | Right | Why |
|-------|-------|-----|
| `ok, data = await ALPHA_LAB_CLIENT.search()` | `data = await ALPHA_LAB_CLIENT.search()` | Client returns data directly, not tuples |
| `scan_type="tweets"` | `scan_type="twitter_post"` | Use exact type strings |
| `sort="score"` | `sort="-insightfulness_score"` | Must use valid sort field with optional `-` prefix |
| `limit=500` | `limit=200` | Max 200 per request; paginate with `offset` |

## Valid Sort Fields (Python client only)

`"insightfulness_score"`, `"-insightfulness_score"` (desc), `"created"`, `"-created"` (desc)

MCP tool always sorts by `-insightfulness_score`.

## Valid Scan Types

`"twitter_post"`, `"defi_llama_chain_flow"`, `"defi_llama_overview"`, `"defi_llama_protocol"`, `"delta_lab_top_apy"`, `"delta_lab_best_delta_neutral"`

## MCP Tool Placeholders

Use `_` for unused string params and `all` for no type filter.

```
research_search_alpha(query, scan_type, created_after, created_before, limit)
```

Examples:
```
research_search_alpha(limit="20")                                          # Top 20 insights
research_search_alpha(created_after="2026-03-06T00:00:00Z", limit="20")    # Today's insights
research_search_alpha(query="ETH", scan_type="twitter_post", limit="10")   # ETH tweets
```

## Don't query Delta Lab for "alpha" requests

Alpha Lab already includes `delta_lab_top_apy` and `delta_lab_best_delta_neutral` scan types. When the user asks for "today's alpha" or "latest insights", use Alpha Lab alone. Only query Delta Lab directly for raw rates, historical timeseries, or detailed screening.
