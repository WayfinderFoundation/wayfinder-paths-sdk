# Definitive adapter

Resting onchain token orders through [Definitive Flash](https://flash.definitive.fi/docs/getting-started),
via the Wayfinder backend relay (which holds the integrator key).

- `build_flash_request(order_type, chain_id=, from_token=, to_token=, amount=, ...)` —
  the relay body for `limit_buy`, `limit_sell`, `stop_loss`, `take_profit`,
  `breakout_buy`, `trailing_stop`, `bracket`.
- `DefinitiveAdapter.place_order(chain_id, request, spent_amount_raw)` — quote,
  bounded approval if needed, check the EIP-712 order, sign, submit.
- `DefinitiveAdapter.list_orders(include_closed=False)` /
  `DefinitiveAdapter.cancel_order(order_id)`.

Signing needs `sign_callback` (approval tx) and `sign_hash_callback` (order and
cancel digests); EIP-7702 Kernel accounts get the Kernel EIP-1271 wrapping.
See `.claude/skills/using-definitive-adapter` for order semantics and gotchas.
