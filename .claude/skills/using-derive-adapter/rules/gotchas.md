# Derive Gotchas

## Derive Wallet Is Not Always The EOA

Derive private REST auth uses `X-LyraWallet`, which the docs describe as the Derive wallet/account address, not necessarily the original owner EOA. Session keys can sign private requests, but the header wallet should still identify the Derive account.

When in doubt, pass both:

```python
adapter = await get_adapter(
    DeriveAdapter,
    "session-key-label",
    derive_wallet_address="0xDeriveAccountWallet",
)
```

## Read-Only Session Keys Cannot Trade

Derive session-key scopes matter:

- `read_only`: account info, orders, positions, history.
- `account`: account-level settings and RFQs, but not trading.
- `admin`: orders, cancel, deposit, withdraw, transfer, and other sensitive actions.

If an order or cancel fails with an auth/scope error, do not retry blindly. Check the key scope.

## Deposit Is Not A Bridge

`deposit_collateral(...)` calls Derive `private/deposit`, which moves collateral from the Derive wallet into a Derive subaccount. It does not bridge assets from Ethereum, Arbitrum, Optimism, or Base to Derive Chain, and it does not submit ERC20 approval transactions.

Before depositing a positive amount, confirm the wallet has the asset on Derive Chain and that the Derive DepositModule has allowance to spend it. Use Derive bridge/approval docs for those steps.

## Lifecycle Writes Are Signed Actions

The adapter builds Derive SignedAction hashes for `create_subaccount`, `deposit_collateral`, `withdraw_collateral`, and `transfer_erc20`, then asks `sign_hash_callback` to sign them. If `sign_hash_callback` is missing, do not fall back to a raw signature string unless the user already has a verified Derive action signature from current docs/client tooling.

`ensure_subaccount(create_if_missing=True, amount="0")` creates an empty subaccount. A positive amount also initiates a deposit.

`ensure_subaccount(create_account_if_missing=True, ...)` can call `public/create_account_with_secret` first, but only when the user provides the current Derive account secret/code details. Do not silently invent onboarding secrets or assume an EOA is already a Derive smart contract wallet.

## Signed Orders Still Need Instrument Metadata

`sign_order(...)` and `place_order(...)` sign through Wayfinder's wallet callback, but they do not derive `max_fee`, choose side/size, or infer Derive asset metadata. Use current instrument/ticker data to provide `asset_address` and `asset_sub_id`, and verify all order fields against Derive constraints before signing.

`submit_order(...)` still accepts a complete pre-signed order payload for callers using Derive's own SDK/client tooling.

## Ticker Versus Orderbook

REST `public/get_tickers` gives best bid/ask, mark, index, stats, and option pricing fields. Full orderbook depth is a WebSocket channel named like:

```text
orderbook.{instrument_name}.{group}.{depth}
```

The adapter currently builds this channel name but does not maintain WebSocket subscriptions.

## Gorlami Limitation

Gorlami tests in this repo simulate EVM contract calls on forked chains. Derive option discovery, account reads, order debug, order submission, and cancel are Derive API/CLOB workflows, with settlement handled by Derive's matching/protocol pipeline. There is no current repo helper that can fork-simulate those API workflows on Derive Chain 957.

Use adapter unit tests, lifecycle history reads, and `submit_order(..., dry_run=True)` as deterministic validation before any live submit.

## Settlement And Margin

Derive options are European and settle to a 30 minute TWAP. Standard margin is centered around zero, and maintenance margin below zero is liquidatable. Portfolio margin can reduce margin requirements but is constrained to one denominated base asset per portfolio margin account.
