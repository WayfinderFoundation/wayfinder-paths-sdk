# DeriveAdapter

Derive options, perps, and spot REST adapter for Wayfinder Paths.

- **Type**: `DERIVE`
- **Module**: `wayfinder_paths.adapters.derive_adapter.adapter.DeriveAdapter`
- **Default API**: `https://api.lyra.finance`
- **Demo API**: `https://api-demo.lyra.finance`

## Scope

This adapter emphasizes Derive options workflows:

- Public discovery: active option instruments, expiries, strikes, fee/tick constraints.
- Public quote reads: `public/get_tickers` and `public/get_ticker`.
- Account reads: subaccounts, subaccount aggregate state, positions, open orders, margin simulation.
- Account lifecycle: account/subaccount detection, optional account creation with invite secret, and signed subaccount creation.
- Collateral lifecycle: signed deposit, signed withdraw, deposit/withdraw history reads, and signed ERC20 collateral transfer between subaccounts.
- Order workflows: signed order debug and signed order submission pass-through, plus cancel.

Full WebSocket streaming, position transfers, RFQs, liquidation actions, session-key management, bridge flows into Derive Chain, ERC20 approval transaction construction, and direct on-chain Derive Chain contract calls are intentionally out of scope.

## Auth And Signing

Derive private REST endpoints require these headers:

- `X-LyraWallet`: the Derive wallet/account address.
- `X-LyraTimestamp`: current UTC timestamp in milliseconds.
- `X-LyraSignature`: standard Ethereum message signature of the timestamp.

When constructed through `get_adapter(DeriveAdapter, "main")`, Wayfinder can auto-wire `sign_hash_callback` and `wallet_address`. If your Derive wallet differs from the signing wallet/session key, pass `derive_wallet_address` explicitly.

Account creation, deposits, withdrawals, transfers, and order submission are self-custodial. Derive requires endpoint authentication plus a signed action payload.

For account/collateral lifecycle methods, the adapter builds the documented Derive `SignedAction` hash and asks Wayfinder's `sign_hash_callback` to sign it. For order submission, use `sign_order(...)` or `place_order(...)` when you have verified the instrument asset address/sub-id, max fee, direction, amount, and user intent. `submit_order(...)` also accepts a complete pre-signed Derive order payload for callers using Derive's own SDK.

Deposits may require the wallet to already hold collateral on Derive Chain and to have approved the Derive DepositModule to spend the asset. Derive bridging and ERC20 approval transactions are documented separately by Derive and are not hidden inside this REST adapter.

## Usage

```python
from wayfinder_paths.adapters.derive_adapter import DeriveAdapter

adapter = DeriveAdapter()

ok, options = await adapter.list_options(currency="ETH")
ok, expiries = await adapter.list_option_expiries(currency="ETH")
ok, tickers = await adapter.get_option_tickers(
    currency="ETH",
    expiry_date=expiries[0]["expiry_date"],
)
```

Authenticated reads:

```python
from wayfinder_paths.adapters.derive_adapter import DeriveAdapter
from wayfinder_paths.mcp.scripting import get_adapter

adapter = await get_adapter(
    DeriveAdapter,
    "main",
    derive_wallet_address="0xYourDeriveWallet",
)

ok, subaccounts = await adapter.get_subaccounts()
ok, account = await adapter.get_account()
ok, positions = await adapter.get_positions(subaccount_id=12345)
ok, margin = await adapter.get_margin(subaccount_id=12345)
```

Ensure/create a usable account and subaccount:

```python
ok, account_state = await adapter.ensure_subaccount()
if not ok:
    ok, account_state = await adapter.ensure_subaccount(
        create_if_missing=True,
        amount="0",
        asset_name="USDC",
        margin_type="SM",
    )
```

If the Derive account itself is missing and the user has a current Derive invite secret, the same flow can create the account first:

```python
ok, account_state = await adapter.ensure_subaccount(
    create_account_if_missing=True,
    account_secret="invite-secret",
    create_if_missing=True,
    amount="0",
    asset_name="USDC",
    margin_type="SM",
)
```

Deposit, withdraw, and inspect lifecycle status:

```python
ok, deposit = await adapter.deposit_collateral(
    subaccount_id=12345,
    amount="100",
    asset_name="USDC",
)

ok, deposit_history = await adapter.get_deposit_history(subaccount_id=12345)

ok, withdrawal = await adapter.withdraw_collateral(
    subaccount_id=12345,
    amount="25",
    asset_name="USDC",
)

ok, withdrawal_history = await adapter.get_withdrawal_history(subaccount_id=12345)
```

Transfer collateral between Derive subaccounts:

```python
ok, transfer = await adapter.transfer_erc20(
    subaccount_id=12345,
    recipient_subaccount_id=67890,
    amount="10",
    asset_name="USDC",
)
```

Callback-signed order dry-run before live submit:

```python
unsigned_order = {
    "subaccount_id": 12345,
    "instrument_name": "ETH-20260522-2500-C",
    "direction": "buy",
    "amount": "0.1",
    "limit_price": "20",
    "max_fee": "2",
    "order_type": "limit",
    "time_in_force": "gtc",
}

ok, order = await adapter.sign_order(
    unsigned_order,
    asset_address="0xInstrumentBaseAssetAddress",
    asset_sub_id=123456789,
)

ok, debug = await adapter.submit_order(order, dry_run=True)
if ok:
    ok, result = await adapter.submit_order(order)
```

## Derive Docs Used

- https://docs.derive.xyz/llms.txt
- https://docs.derive.xyz/docs/about-derive.md
- https://docs.derive.xyz/docs/supported-products-1.md
- https://docs.derive.xyz/docs/standard-margin-1.md
- https://docs.derive.xyz/docs/portfolio-margin-1.md
- https://docs.derive.xyz/docs/settlements.md
- https://docs.derive.xyz/docs/lyra-chain.md
- https://docs.derive.xyz/reference/overview.md
- https://docs.derive.xyz/reference/json-rpc.md
- https://docs.derive.xyz/reference/authentication.md
- https://docs.derive.xyz/reference/session-keys.md
- https://docs.derive.xyz/reference/rate-limits.md
- https://docs.derive.xyz/reference/protocol-constants.md
- https://docs.derive.xyz/reference/create-or-deposit-to-subaccount.md
- https://docs.derive.xyz/reference/deposit-to-lyra-chain.md
- https://docs.derive.xyz/reference/on-chain-withdraw.md
- https://docs.derive.xyz/reference/post_private-get-account.md
- https://docs.derive.xyz/reference/public-create_account_with_secret-1.md
- https://docs.derive.xyz/reference/post_private-create-subaccount.md
- https://docs.derive.xyz/reference/post_private-deposit.md
- https://docs.derive.xyz/reference/post_private-withdraw.md
- https://docs.derive.xyz/reference/post_private-get-deposit-history.md
- https://docs.derive.xyz/reference/post_private-get-withdrawal-history.md
- https://docs.derive.xyz/reference/post_private-transfer-erc20.md
- https://docs.derive.xyz/reference/transfer-collateral.md
- https://pypi.org/project/derive_action_signing/
- https://github.com/derivexyz/v2-action-signing-python
- https://docs.derive.xyz/reference/post_public-get-instruments.md
- https://docs.derive.xyz/reference/post_public-get-tickers.md
- https://docs.derive.xyz/reference/orderbook-instrument_name-group-depth.md
- https://docs.derive.xyz/reference/post_private-order-debug.md
- https://docs.derive.xyz/reference/post_private-order.md
- https://docs.derive.xyz/reference/post_private-cancel.md

## Testing

```bash
poetry run pytest -o addopts= wayfinder_paths/adapters/derive_adapter -q
```
