# Data sources (what to use, where it comes from)

## Golden rule

Strategies should call **adapters** for domain actions. Clients are low-level wrappers.

Data flow: `Strategy → Adapter → Client(s) → Network/API`

## Data accuracy (no guessing)

- Never invent or "ballpark" rates/APYs/funding, even if they seem stable.
- Prefer a concrete adapter/client/tool call. If you can't fetch, say "unavailable" and show the exact call needed.
- Before searching external docs, consult this repo's own adapter/client surfaces (and their `manifest.yaml` + `examples.json`) first.

## Finding the right data source

Load the protocol-specific skill from the table in CLAUDE.md (e.g. `/using-hyperliquid-adapter`, `/using-pendle-adapter`, `/using-pool-token-balance-data`, `/using-delta-lab`). Each skill documents the canonical read methods, return shapes, and gotchas for its surface — don't enumerate them here, they rot.
