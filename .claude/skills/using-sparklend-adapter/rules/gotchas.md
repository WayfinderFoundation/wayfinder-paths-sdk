# SparkLend gotchas

- **Ethereum-only in this repo:** `SPARKLEND_BY_CHAIN` currently only contains `chain_id=1`.
- **`get_full_user_state(...)` is single-chain:** it requires `chain_id` and is not the same cross-chain shape as Aave V3's aggregate helper.
- **Method signatures differ from Aave V3:** SparkLend `borrow(...)` and `repay(...)` take `asset` / `amount`, not `underlying_token` / `qty`.
- **Raw units only:** all write methods expect integer base units, not floats or human-readable token amounts.
- **Native handling is split:** use `ZERO_ADDRESS` with `lend(...)` / `unlend(...)`, but use `borrow_native(...)` / `repay_native(...)` for native debt flows.
- **Collateral on native supply uses wrapped native:** pass the wrapped-native reserve address to `set_collateral(...)`, not `ZERO_ADDRESS`.
- **Stable rate borrow is conditional:** `rate_mode=1` only works if that reserve has stable borrowing enabled.
- **`withdraw_full` / `repay_full` use max semantics:** pass `qty=0` or `amount=0` only when the corresponding full flag is `True`.
- **Rewards docs should stay narrow:** this adapter exposes `claim_rewards(chain_id)` for claiming, but the read methods documented here do not return rewards APR fields.
