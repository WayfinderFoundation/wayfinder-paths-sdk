# Placing and managing token orders

## MCP tools

- `definitive_place_order(wallet_label, order_type, from_token, to_token, amount, ...)`
  places a resting order (review-gated). `amount` is a decimal string of
  `from_token` (e.g. `"25.0"`), like `onchain_swap`.
- `definitive_list_orders(wallet_label, include_closed=False)` lists the
  wallet's orders (read-only).
- `definitive_cancel_order(wallet_label, order_id)` cancels by id
  (review-gated). Nothing moves; the wallet signs the cancel.

## Order types

Buy types spend `from_token` to buy `to_token`; every other type sells
`from_token`. Prices are the **USD price of the token bought or sold** — not
a pair rate.

| `order_type` | Side | Price args | Fires when |
| --- | --- | --- | --- |
| `limit_buy` | buy `to_token` | `limit_price` | price ≤ limit |
| `limit_sell` | sell `from_token` | `limit_price` | price ≥ limit |
| `stop_loss` | sell | `stop_loss_price` | price falls to it (then market) |
| `take_profit` | sell | `take_profit_price` | price rises to it (then market) |
| `breakout_buy` | buy | `trigger_price` | price rises to it (then market) |
| `trailing_stop` | sell | `trail_percent` (0.5–50), optional `activation_price` | price pulls back `trail_percent` from its high |
| `bracket` | sell | `take_profit_price` + `stop_loss_price` | whichever comes first; the other is cancelled |

Same chain only (Ethereum, Optimism, BSC, Polygon, HyperEVM, Base,
Arbitrum, Avalanche). Read the current price first (e.g.
`research_search_price`) and set triggers on the correct side of it.

## Reading orders

Each order has `orderType`, `side`, `status`, `qty` (in the spent token),
`targetAsset` / `contraAsset`, `limitNotionalPrice`, `trigger`, `brackets`,
`trailingStop` (`activated`, `watermarkPrice`, `effectiveTriggerPrice`) and,
once closed, `closeReason`. Open statuses: `ORDER_STATUS_PENDING`,
`ORDER_STATUS_ACCEPTED`, `ORDER_STATUS_PARTIALLY_FILLED`.
