# Compound gotchas

- **Comet-only scope:** this adapter is for Compound III / Comet markets configured in `COMPOUND_COMET_BY_CHAIN`. Do not describe Compound v2 cToken flows, liquidations, absorbs, collateral purchases, or governance actions as supported here.
- **Supported networks are narrow:** in this repo the Compound adapter is only configured for Ethereum, Base, Arbitrum, and Polygon. Do not describe Optimism, Scroll, Linea, Ronin, Unichain, or Mantle as supported.
- **Raw units only:** all write methods expect integer base units, not floats or human-readable token amounts.
- **Base-token methods are strict:** `lend(...)`, `unlend(...)`, `borrow(...)`, and `repay(...)` require the correct `base_token` for that specific `comet`. The adapter validates it against on-chain `baseToken()`.
- **Collateral methods are separate:** use `supply_collateral(...)` and `withdraw_collateral(...)` for collateral assets. Do not try to pass collateral assets into the base-token methods.
- **One market needs one Comet address:** reads and writes are keyed by `chain_id` plus the exact `comet` address, not by market symbol alone.
- **Borrow is base-only:** `borrow(...)` borrows the Comet market's base asset by calling `withdraw(base_token, amount)`. It does not borrow arbitrary collateral assets.
- **Borrow minimum is enforced:** `borrow(...)` fails if `amount < base_borrow_min`.
- **Full-withdraw and full-repay differ:** `unlend(..., withdraw_full=True)` and `repay(..., repay_full=True)` use `MAX_UINT256`, but `withdraw_collateral(..., withdraw_full=True)` first reads the exact collateral balance and withdraws that amount.
- **`amount=0` is only valid with the full flags:** `amount=0` is rejected unless `withdraw_full=True` or `repay_full=True` as appropriate.
- **Rewards are per Comet rewards contract:** `claim_rewards(...)` fails if the configured rewards contract has no reward token for that market.
- **`claim_rewards(...)` returns a tx hash:** it does not return a dict of claimed token amounts.
- **`get_pos(...)` requires an explicit account:** unlike `get_full_user_state(...)`, it does not fall back to `wallet_address`.
- **Price fields are optional:** if `include_prices=False`, do not assume USD values or price-derived fields are populated.
