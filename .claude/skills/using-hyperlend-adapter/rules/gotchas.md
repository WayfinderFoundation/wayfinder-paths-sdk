# Hyperlend gotchas

## Data accuracy (no guessing)

- Hyperlend “rates” must come from Hyperlend endpoints (e.g. `market_entry`, `lend_rate_history`) and may change frequently.
- Do **not** claim extra yield sources (e.g. “~3–4% staking APY”) unless you fetched them from a concrete source in this repo.

## Units

- HyperlendAdapter expects **raw ints** (`qty`) for on-chain calls.
- Don’t pass floats or “human” values directly.

## RPC requirements

All on-chain execution requires RPC URLs to be resolvable:
- by default, via the SDK's authenticated Wayfinder RPC proxy
- only use `config.json` `strategy.rpc_urls` for explicit local/fork overrides
