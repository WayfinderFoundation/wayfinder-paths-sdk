# What is Alpha Lab?

Alpha Lab is a **scored alpha insight feed** that surfaces actionable DeFi signals. It aggregates and scores insights from multiple scan types, ranking them by an `insightfulness_score` (0-1).

## Scan Types

| Type | What it surfaces |
|------|-----------------|
| `twitter_post` | Scored tweets from DeFi/crypto accounts |
| `defi_llama_chain_flow` | Notable chain TVL inflows/outflows |
| `defi_llama_overview` | DeFi ecosystem overview snapshots |
| `defi_llama_protocol` | Individual protocol highlights |
| `delta_lab_top_apy` | Standout APY opportunities from Delta Lab |
| `delta_lab_best_delta_neutral` | Top delta-neutral pair opportunities |

Use `await ALPHA_LAB_CLIENT.get_types()` or `research_get_alpha_types()` to discover available types at runtime.

## Key Concepts

- **Insightfulness score** — 0-1 float. Higher = more actionable/notable. Use `min_score` to filter noise (Python client only).
- **Read-only** — Alpha Lab is discovery only, no execution.
- **Already includes Delta Lab highlights** — Alpha Lab surfaces top APYs and delta-neutral pairs from Delta Lab. Don't query Delta Lab separately for alpha requests. Use Delta Lab directly only for raw rates, timeseries, or detailed screening.
