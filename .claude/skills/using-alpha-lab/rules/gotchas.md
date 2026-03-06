# Gotchas

## Quick Cheat Sheet

| Wrong | Right | Why |
|-------|-------|-----|
| `ok, data = await ALPHA_LAB_CLIENT.search()` | `data = await ALPHA_LAB_CLIENT.search()` | Client returns data directly, not tuples |
| `scan_type="tweets"` | `scan_type="twitter_post"` | Use exact type strings |
| `sort="score"` | `sort="-insightfulness_score"` | Must use valid sort field with optional `-` prefix |
| `limit=500` | `limit=200` | Max 200 per request; paginate with `offset` |

## Valid Sort Fields

`"insightfulness_score"`, `"-insightfulness_score"` (desc), `"created"`, `"-created"` (desc)

## Valid Scan Types

`"twitter_post"`, `"defi_llama_chain_flow"`, `"delta_lab_top_apy"`, `"delta_lab_best_delta_neutral"`

## MCP URI Placeholders

Use `_` for unused string params and `all` for no type filter. Example:
`wayfinder://alpha-lab/search/_/all/0/_/_/-insightfulness_score/20/0`
