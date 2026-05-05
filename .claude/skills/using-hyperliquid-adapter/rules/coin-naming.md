# Hyperliquid coin names — canonical forms across surfaces

Hyperliquid has four trading surfaces. Each has one canonical coin string. The string itself tells the resolver which surface — there is no `is_spot`/`is_perp` flag to pass alongside (the resolver still accepts `is_spot` for `place_order` as a sanity check, but the format is what dispatches).

| Surface | What you pass as `coin` | Example | API key (l2Book / mids / trades) | asset_id range |
|---|---|---|---|---|
| **Default perp** | bare symbol | `BTC`, `ETH`, `HYPE` | same as input (`BTC`) | `0`–`9_999` |
| **HIP-3 dex perp** | `<dex>:<TICKER>` | `xyz:NVDA`, `hyna:BTC`, `vntl:SPACEX` | same as input (`xyz:NVDA`) | `100_000`+ (per-dex bands of 10k: 110k, 120k, …) |
| **Spot** | `<BASE>/<QUOTE>` — full pair, never bare | `BTC/USDC`, `BTC/USDH`, `USDH/USDC`, `PURR/USDC` | `@<spot_index>` (or `PURR/USDC` for index 0) | `10_000`–`99_999` |
| **HIP-4 outcome** (book / mids / trades) | `#<encoding>` | `#20` (YES on outcome 2), `#21` (NO on outcome 2) | `#<encoding>` | `100_000_000`+ |
| **HIP-4 outcome** (spotClearinghouseState balances) | `+<encoding>` | `+20`, `+21` | priced via `#<encoding>` | n/a (balance entry, not a market id) |

Outcome `encoding = 10 * outcome_id + side`. Asset id = `OUTCOME_ASSET_OFFSET + encoding`.

## Why spot is `BASE/QUOTE`, not just `BASE`

Many tokens trade against multiple quotes — `BTC/USDC` and `BTC/USDH` are both live, with separate orderbooks. Passing just `BTC` is ambiguous. The resolver will not guess.

## Why outcomes have two prefixes

- `#<n>` — the **market** id, used by `l2Book`, `trades`, `allMids`. Always present in mid-price responses.
- `+<n>` — the **balance entry** id, used by `spotClearinghouseState.balances[].coin`. Read-only — you receive these in user state but never send them in an order.

The resolver accepts either form on input; if you pass `+20` it normalizes to `#20` for pricing/book lookups.

## How `coin` and `asset_id` interact

`hyperliquid_execute(action="place_order", ...)` accepts either `coin` or `asset_id`. Pass one, not both.

- `coin` is preferred for clarity. Use the canonical form for the surface.
- `asset_id` short-circuits the lookup. Validate the range yourself if you're constructing it.
- For HIP-4 outcomes, do not use `place_order` — call `place_outcome_order(outcome_id=..., side=..., ...)` instead. Outcome trading has different rules (zero-fee, integer contracts only, USDH collateral, no leverage), so it has its own action.

## Display synonyms (presentation only — not resolved by SDK)

Hyperliquid has wrapped variants of common assets in spot. The UI typically shows the unwrapped ticker; the resolver requires the on-chain name.

| Display name | Canonical spot pair |
|---|---|
| `ETH/USDC` | `UETH/USDC` |
| `BTC/USDC` | `UBTC/USDC` (when "BTC" is shorthand for wrapped) |
| `SPX` | `SPX6900` |

If the user types `ETH/USDC`, translate to `UETH/USDC` before calling the tool. The resolver does not do this for you — synonym handling is a presentation concern, not a resolution concern.

## Sanity-checklist before placing an order

- Spot order? `coin` contains exactly one `/`.
- HIP-3 perp? `coin` contains exactly one `:`.
- Default perp? `coin` is bare symbol with no `/`, `:`, `#`, or `+`.
- Outcome? You're calling `place_outcome_order`, not `place_order`.

If any of these are wrong, the resolver returns a structured error — see the error `code` field, not just the human message. Codes: `invalid_request` (format wrong / missing field), `not_found` (format ok but coin doesn't exist on this surface).
