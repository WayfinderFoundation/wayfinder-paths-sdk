# Derive Reads

## Public Options Discovery

Use the adapter for live Derive data. Do not invent expiries, strikes, fees, quotes, funding, or margin values.

```python
from wayfinder_paths.adapters.derive_adapter import DeriveAdapter

adapter = DeriveAdapter()

ok, options = await adapter.list_options(currency="ETH", expired=False)
ok, expiries = await adapter.list_option_expiries(currency="ETH")
ok, tickers = await adapter.get_option_tickers(
    currency="ETH",
    expiry_date=expiries[0]["expiry_date"],
)
```

Useful fields from `list_options(...)`:

- `instrument_name`: Derive instrument name, for example `ETH-20260522-2500-C`.
- `option_details.expiry`: Unix expiry timestamp in seconds.
- `option_details.strike`: strike as a decimal string.
- `option_details.option_type`: `C` or `P`.
- `tick_size`, `minimum_amount`, `amount_step`: order sizing constraints.
- `maker_fee_rate`, `taker_fee_rate`, `base_fee`, `max_fee`: fee inputs for order signing and risk review.

Useful fields from `get_option_tickers(...)`:

- `b` / `B`: best bid price and amount.
- `a` / `A`: best ask price and amount.
- `M`: mark price.
- `I`: index price.
- `stats.oi`: open interest.
- `option_pricing`: greeks and model fields exposed by Derive.

## Orderbook Channel

Full orderbook snapshots are exposed as WebSocket channels, not REST.

```python
channel = DeriveAdapter.orderbook_channel(
    "ETH-20260522-2500-C",
    group="1",
    depth="10",
)
```

The adapter currently builds the documented channel name and reads REST ticker quotes. It does not run a WebSocket subscription loop.

## Authenticated Reads

Private reads require Derive REST auth. If the signing wallet/session key differs from the Derive account wallet, pass `derive_wallet_address`.

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
ok, subaccount = await adapter.get_subaccount(subaccount_id=12345)
ok, positions = await adapter.get_positions(subaccount_id=12345)
ok, orders = await adapter.get_open_orders(subaccount_id=12345)
ok, margin = await adapter.get_margin(subaccount_id=12345)
```

Useful account fields:

- `subaccount_ids`: Derive subaccounts owned by the authenticated wallet.
- `fee_info`: current fee configuration for options/perps/spot/RFQ.
- `cancel_on_disconnect`: account-level cancellation behavior.
- `websocket_*_tps`: current account WebSocket rate-limit tiers.

For trade previews, prefer `get_margin(..., simulated_position_changes=[...])` before considering any order submission.

## Deposit And Withdrawal History

Use history reads after a lifecycle write returns `status="requested"` and a `transaction_id`.

```python
ok, deposits = await adapter.get_deposit_history(
    subaccount_id=12345,
    start_timestamp=0,
)

ok, withdrawals = await adapter.get_withdrawal_history(subaccount_id=12345)
```

Watch `tx_status` for `requested`, `pending`, `settled`, `reverted`, `ignored`, or `timed_out`. Check `error_log` before retrying a failed deposit or withdrawal.
