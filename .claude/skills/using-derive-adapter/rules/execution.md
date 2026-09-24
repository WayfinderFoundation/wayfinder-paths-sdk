# Derive Execution

Derive order submission is a trading action. It is not just an HTTP request.

Derive requires two signatures for sensitive workflows:

- Endpoint authentication: `X-LyraWallet`, `X-LyraTimestamp`, `X-LyraSignature`.
- Action signature: the signed payload fields sent to create/deposit/withdraw/transfer/order endpoints.

The adapter handles endpoint auth when a signing callback is configured. For account lifecycle, collateral lifecycle, and simple order helpers, it also builds the documented Derive `SignedAction` hash and signs it with Wayfinder's `sign_hash_callback`. You still must verify instrument constraints, fees, amount, side, and user intent before asking the adapter to sign.

## Ensure Or Create A Subaccount

Start with a read. Only create if the user explicitly wants a new subaccount.

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

`create_if_missing=True` calls `private/create_subaccount`. It changes account state and requires admin/session-key permission. `amount="0"` creates an empty Standard Margin subaccount. A positive amount also initiates a deposit and requires the wallet to have collateral and allowance available on Derive Chain.

If the Derive account itself is missing, the adapter can run Derive's `public/create_account_with_secret` flow first when the user has a current invite secret:

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

Without `account_secret`, do not guess onboarding details. Ask the user to create/onboard the Derive account through Derive's current interface or provide the documented secret/code.

## Deposit And Withdraw Collateral

Deposit collateral into a subaccount:

```python
ok, deposit = await adapter.deposit_collateral(
    subaccount_id=12345,
    amount="100",
    asset_name="USDC",
)
```

Withdraw collateral back to the Derive wallet:

```python
ok, withdrawal = await adapter.withdraw_collateral(
    subaccount_id=12345,
    amount="25",
    asset_name="USDC",
)
```

Then poll history:

```python
ok, deposits = await adapter.get_deposit_history(subaccount_id=12345)
ok, withdrawals = await adapter.get_withdrawal_history(subaccount_id=12345)
```

Do not assume a `requested` response is final settlement. Check `tx_status` until it is `settled` or a terminal failure.

## Transfer Collateral Between Subaccounts

Use `transfer_erc20(...)` for ERC20 collateral transfers between Derive subaccounts owned by the same Derive wallet.

```python
ok, transfer = await adapter.transfer_erc20(
    subaccount_id=12345,
    recipient_subaccount_id=67890,
    amount="10",
    asset_name="USDC",
)
```

This signs both sender and recipient action details. Position transfers use separate Derive endpoints and are not part of this adapter surface.

## Callback-Signed Order Dry-Run

Use `dry_run=True` first. This calls `private/order_debug`, which Derive documents as read-only.

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
```

Only submit live after the user has reviewed the instrument, side, size, price, max fee, margin impact, expiry, and account/subaccount.

```python
ok, result = await adapter.submit_order(order)
```

You can also combine signing and submission:

```python
ok, debug = await adapter.place_order(
    unsigned_order,
    asset_address="0xInstrumentBaseAssetAddress",
    asset_sub_id=123456789,
    dry_run=True,
)
```

Required unsigned order fields for `sign_order(...)`:

- `amount`
- `direction`
- `instrument_name`
- `limit_price`
- `max_fee`
- `subaccount_id`

Additional signing inputs:

- `asset_address`: Derive instrument base asset address from instrument/ticker metadata.
- `asset_sub_id`: Derive instrument sub ID from instrument/ticker metadata.

Required signed order fields for raw `submit_order(...)`:

- `amount`
- `direction`
- `instrument_name`
- `limit_price`
- `max_fee`
- `nonce`
- `signature`
- `signature_expiry_sec`
- `signer`
- `subaccount_id`

## Cancel

Cancelling is an admin-scoped private endpoint.

```python
ok, result = await adapter.cancel_order(
    instrument_name="ETH-20260522-2500-C",
    order_id="...",
    subaccount_id=12345,
)
```

Confirm the order belongs to the intended subaccount and instrument before cancelling. Use `get_open_orders(subaccount_id=...)` first.
