# Boros Adapter

Adapter for Boros fixed-rate markets, margin accounts, and LP vaults on Arbitrum.

- **Type**: `BOROS`
- **Module**: `wayfinder_paths.adapters.boros_adapter.adapter.BorosAdapter`

## Usage

```python
from wayfinder_paths.adapters.boros_adapter import BorosAdapter

adapter = BorosAdapter(config={})
```

## Query Markets

### get_all_markets

Returns the normalized Boros market list with nested live rates, optional vault summary, and compact historical context.

```python
ok, markets = await adapter.get_all_markets(
    active_only=True,
    history_points=24,
)

if ok:
    for market in markets:
        print(
            market["market_id"],
            market["symbol"],
            market["rates"]["floating_apr"],
            market["rates"]["mark_apr"],
            market["rates"]["vault_apy"],
        )
```

Per-market output includes:
- top-level market metadata and status: `market_id`, `market_address`, `symbol`, `underlying_symbol`, `platform`, `collateral`, `state`, `is_active`, `maturity_ts`, `tenor_days`
- nested `rates`: live `floating_apr`, `mark_apr`, `vault_apy`, plus market stats like `mid_apr`, `best_bid_apr`, `best_ask_apr`, funding moving averages, volume, and OI
- nested `vault`: `apy`, `expiry`, `collateral_symbol`, `tvl`, `tvl_usd`, `available_tokens`, `available_usd`
- nested `history`: compact summary with latest and average mark/floating rates plus latest funding moving averages

Useful flags:
- `include_vault_summary=False` for a lighter market+rates view
- `include_history_summary=False` to skip per-market history calls
- `account="0x..."` to include `vault.user` balances for that wallet

When `account` is provided, the adapter also appends inactive vault-only rows for expired markets that have rolled off live `/markets` but still have real user LP/deposit exposure. Those rows may use fallback symbols like `BOROS-MARKET-34` if Boros no longer returns market metadata for that expired ID.
Rows are not appended for `availableBalanceToDeposit` alone, because that would duplicate idle collateral across many expired vault IDs.

## Query Vaults

### get_vaults_summary

Returns the current Boros vault set. When `account` is provided, the adapter also attaches the user's LP balances and estimated deposited value.

```python
ok, vaults = await adapter.get_vaults_summary(account="0x...")

if ok:
    for vault in vaults:
        print(
            vault.market_id,
            vault.symbol,
            vault.apy,
            vault.tenor_days,
            vault.is_isolated_only,
        )
```

### search_vaults

Filters the vault summary by collateral token ID and/or asset symbol.

```python
ok, usdt_vaults = await adapter.search_vaults(
    token_id=3,
    asset="ETH",
    account="0x...",
)
```

### best_yield_vault

Selects the highest-APY vault that is currently depositable, has enough remaining capacity, and satisfies the tenor filter.

Use `allow_isolated_only=True` if you want isolated-only vaults included in the search.

```python
ok, best = await adapter.best_yield_vault(
    token_id=3,
    amount_tokens=1_000.0,
    min_tenor_days=7.0,
    allow_isolated_only=True,
)
```

### is_vault_open_for_deposit

Helper for checking whether a vault is depositable under the current policy.

```python
is_open = adapter.is_vault_open_for_deposit(
    best,
    min_tenor_days=7.0,
    allow_isolated_only=True,
)
```

## Deposit To A Vault

Boros vault deposits are a two-step flow:

1. Deposit collateral into the correct Boros margin bucket.
2. Convert that collateral amount into Boros scaled cash and add it to the vault.

### Cross-margin vault deposit

```python
token_id = 3  # example: USDT on Boros
market_id = 73
amount_native = 1_000 * 10**6  # 1000 USDT in native token decimals
collateral_address = "0x..."

ok, dep = await adapter.deposit_to_cross_margin(
    collateral_address=collateral_address,
    amount_wei=amount_native,
    token_id=token_id,
    market_id=market_id,
)

scaled_cash = await adapter.unscaled_to_scaled_cash_wei(token_id, amount_native)

ok, tx = await adapter.deposit_to_vault(
    market_id=market_id,
    net_cash_in_wei=scaled_cash,
)
```

### Isolated-only vault deposit

For isolated-only vaults, the first step must target the market's isolated margin bucket.

```python
token_id = 3
market_id = 73
amount_native = 1_000 * 10**6
collateral_address = "0x..."

ok, dep = await adapter.deposit_to_isolated_margin(
    collateral_address=collateral_address,
    amount_wei=amount_native,
    token_id=token_id,
    market_id=market_id,
)

scaled_cash = await adapter.unscaled_to_scaled_cash_wei(token_id, amount_native)

ok, tx = await adapter.deposit_to_vault(
    market_id=market_id,
    net_cash_in_wei=scaled_cash,
)
```

## Notes

- `amount_wei` for collateral deposits is in the token's native decimals, not Boros 1e18 cash units. For USDT, `1 USDT = 1_000_000`.
- `net_cash_in_wei` for `deposit_to_vault()` is Boros internal cash scaled to `1e18`. Use `unscaled_to_scaled_cash_wei()` instead of hand-rolling the conversion.
- `deposit_to_vault()` is the normal entry point. It resolves the AMM for the market and handles cross vs isolated router params from vault metadata.
- `deposit_to_vault_direct()` exists for lower-level use when you already know the `amm_id`.

## Return Format

All methods return `(success: bool, data: Any)` tuples.

## Testing

```bash
poetry run pytest wayfinder_paths/adapters/boros_adapter/ -q
```
