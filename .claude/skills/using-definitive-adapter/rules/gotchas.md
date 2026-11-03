# Gotchas

- **Tokens stay in the wallet until the order fills.** If the balance or
  allowance drops below the order (a swap, a send, another order filling),
  Definitive cancels it with `REASON_INSUFFICIENT_FUNDER_BALANCE`. That is
  final: refilling the wallet does not reopen it. Place a new order.
- **A trigger already crossed when placed closes immediately**
  (`REASON_ORDER_TRIGGERED_ON_ENTRY`) instead of executing. Trailing stops are
  the exception.
- **No native gas tokens as the spent token.** Wrap first (ETH → WETH) with
  `onchain_swap`, then place the order with the wrapped token.
- **Trailing stops can't be modified**, only cancelled and re-placed.
  `watermarkPrice` can lag a few seconds behind what actually fires.
- **Cancelling doesn't revoke the approval.** The approval is sized to open
  orders on that token (never unlimited).
- **Fees:** Definitive charges 0.10% on advanced orders (0.20% from 2027);
  stop/trailing exits execute at market with up to 50% price-impact tolerance
  by default.
- **What the adapter checks before signing:** the order Definitive returns
  must be a Flash order on this chain, spending at most `amount` of
  `from_token` from this wallet and paying `to_token` back to this wallet;
  the approval must be for Flash and at most `amount`. A quote that asks for
  a Permit2 permit is refused before any approval is sent. Anything else is
  refused and nothing is signed. Agent wallets with EIP-7702 delegation
  (Kernel) sign through the account's EIP-1271 wrapping automatically.
