# Gotchas

- **Tokens stay in the wallet until the order fills.** If the balance or
  allowance drops below the order (a swap, a send, another order filling),
  Definitive cancels it with `REASON_INSUFFICIENT_FUNDER_BALANCE`. That is
  final: refilling the wallet does not reopen it. Place a new order.
- **A trigger already crossed when placed closes immediately**
  (`REASON_ORDER_TRIGGERED_ON_ENTRY`) instead of executing. Trailing stops are
  the exception.
- **No native gas tokens as the spent token on EVM.** Wrap first (ETH →
  WETH) with `onchain_swap`, then place the order with the wrapped token.
  Solana is the exception: native SOL is wrapped to wSOL automatically.
- **Solana: one delegate per token account.** The order works by delegating
  the token account to Flash. Anything that later sets a different delegate
  on that account (another app, another order venue) silently breaks
  Definitive's exits for it. Placing a new Flash order re-delegates.
- **Solana costs:** the first order per token creates token accounts
  (~0.002 SOL rent each) and a delegate transaction; keep some SOL in the
  wallet. Bought SOL arrives as wSOL.
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
  a Permit2 permit is refused before any approval is sent. On Solana, the
  setup instructions are checked and rebuilt (token accounts, SOL wrap,
  delegate to Flash's `flash_orders` PDA, bounded), and the order message's
  hash is recomputed from the wallet's own token accounts. Anything else is
  refused and nothing is signed. Agent wallets with EIP-7702 delegation
  (Kernel) sign through the account's EIP-1271 wrapping automatically.
