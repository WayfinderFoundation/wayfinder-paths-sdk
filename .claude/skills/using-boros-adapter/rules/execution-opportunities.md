# Boros execution opportunities (writes)

## Pre-trade checklist (MUST run before suggesting or placing any trade)

**Before suggesting or executing ANY Boros trade**, always fetch current state:

```python
# 1. Check existing positions
success, positions = await adapter.get_active_positions()
# Returns list of active rate positions with market_id, side, size, pnl

# 2. Check collateral balances
success, balances = await adapter.get_account_balances(token_id=3)  # or 5 for HYPE
# Returns: {"isolated": 0.0, "cross": 12.83, "total": 12.83, ...}

# 3. Check full collateral state (includes pending withdrawals)
success, collaterals = await adapter.get_collaterals()
# Returns raw collateral data with netBalance, availableBalance, withdrawal status
```

**Why this matters:**
- Don't suggest opening a position if one already exists (may want to adjust instead)
- Don't suggest depositing if collateral is already sufficient
- Don't trade if there's a pending withdrawal (funds are locked)

## Order placement workflow (don't skip steps)

Before placing any Boros order, you **must** have the right collateral on Arbitrum and deposited to Boros:

```
1. Run pre-trade checklist         →  get_active_positions(), get_account_balances(), get_collaterals()
2. Check market's collateral type  →  market["tokenId"] (3=USDT, 5=HYPE)
3. Acquire collateral on Arbitrum  →  swap via BRAP, or OFT bridge for HYPE
4. Check Boros balance             →  get_account_balances(token_id=...)
5. If insufficient, deposit        →  deposit_to_cross_margin(...)
6. Sweep isolated → cross          →  (deposit_to_cross_margin does this automatically)
7. Place order                     →  place_rate_order(...)
```

**Common mistake**: Jumping straight to `place_rate_order()` without checking state or depositing collateral first → order fails or creates duplicate positions.

### Collateral types (token_id)

| token_id | Token | Decimals | How to acquire on Arbitrum |
|----------|-------|----------|---------------------------|
| 1        | WBTC  | 8        | BRAP swap |
| 2        | WETH  | 18       | BRAP swap |
| 3        | USDT  | 6        | BRAP swap to `usdt0-arbitrum` |
| 4        | BNB   | 18       | BRAP swap |
| 5        | HYPE  | 18       | OFT bridge from HyperEVM (see below) |

**Get token addresses dynamically** via `get_assets()` or `get_asset_by_token_id()`:

```python
# Get all assets (cached for 1 hour)
success, assets = await adapter.get_assets()
# Returns: [{"tokenId": 3, "address": "0xfd086...", "symbol": "USD₮0", "decimals": 6, "isCollateral": true}, ...]

# Get specific asset by token_id
success, asset = await adapter.get_asset_by_token_id(token_id=3)
collateral_address = asset["address"]  # "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9"
decimals = asset["decimals"]           # 6
```

**Each market accepts a specific collateral** — check `market["tokenId"]` to know which one.

### Acquiring HYPE collateral (token_id=5)

HYPE collateral requires **Arbitrum OFT HYPE** (LayerZero token). The path is:

```
1. Get native HYPE on HyperEVM (chain 999)
   → BRAP swap: mcp__wayfinder__execute(kind="swap", to_token="hyperliquid-hyperevm", ...)

2. OFT bridge HyperEVM → Arbitrum
   → adapter.bridge_hype_oft_hyperevm_to_arbitrum(amount_wei=...)

3. Wait for bridge completion (LayerZero is async)

4. Deposit OFT HYPE to Boros
   → adapter.deposit_to_cross_margin(..., token_id=5, ...)
```

### Acquiring USDT collateral (token_id=3)

Simpler - just swap to USDT on Arbitrum:

```python
# Swap to USDT on Arbitrum via BRAP
mcp__wayfinder__execute(
    kind="swap",
    wallet_label="main",
    from_token="usd-coin-base",  # or whatever you have
    to_token="usdt0-arbitrum",   # USDT on Arbitrum
    amount="50",
)
```

Then deposit to Boros with `token_id=3`.

## YU Sizing (critical for order placement)

YU (Yield Units) sizing depends on the **collateral token**:

| Collateral | YU Meaning | $50 Position |
|------------|------------|--------------|
| USDT (token_id=3) | 1 YU ≈ $1  | `size_yu = 50` |
| HYPE (token_id=5) | 1 YU = 1 HYPE | `size_yu = 50 / hype_price` |

**Common mistake**: Don't multiply by tenor/365 - that's for yield calculations, not position sizing.

```python
# WRONG: size_yu = notional_usd * tenor_days / 365
# RIGHT: size_yu = notional_usd  (for USDT collateral, token_id=3)

size_yu = 50  # $50 position with USDT collateral
size_yu_wei = int(size_yu * 1e18)  # Convert to wei
```

## Execution surfaces (fund/position risk)

All Boros execution in this repo is **on Arbitrum** (default `chain_id = 42161`).
The HYPE OFT bridge helper runs on **HyperEVM**.

Deposits/withdrawals:
- `deposit_to_cross_margin(collateral_address, amount_wei, token_id, market_id)`
- `withdraw_collateral(token_id, amount_native|amount_wei, account_id=None)`
- `cash_transfer(market_id, amount_wei, is_deposit=False)`
- `sweep_isolated_to_cross(token_id, market_id=None)`

Orders/position management:
- `place_rate_order(market_id, token_id, size_yu_wei, side, limit_tick=None, tif="GTC", slippage=0.05)`
- `cancel_orders(market_id, order_ids=[...])`
- `close_positions_market(market_id, size_yu_wei=None)`
- `close_positions_except(keep_market_id, token_id=..., market_ids=None, best_effort=True)`
- `ensure_position_size_yu(market_id, token_id, target_size_yu, ...)`
- `finalize_vault_withdrawal(token_id, ...)`

**Example `place_rate_order` call:**
```python
success, result = await adapter.place_rate_order(
    market_id=47,
    token_id=3,           # REQUIRED: collateral token (3=USDT, 5=HYPE)
    size_yu_wei=int(70 * 1e18),  # 70 YU
    side="long",          # "long" or "short"
    limit_tick=None,      # Optional: auto-picks tick for fill if None
)
```

On-chain reads (safety rails):
- `get_cash_fee_data(token_id=...)` (reads `MarketHub.getCashFeeData`)

Cross-chain funding:
- `bridge_hype_oft_hyperevm_to_arbitrum(amount_wei, ...)` (native HYPE → Arbitrum OFT HYPE)
  - Before you can bridge, you must have **native HYPE on HyperEVM (chain 999)**.
  - For ad-hoc funding (not running the full delta-neutral strategy), prefer acquiring HyperEVM HYPE via **BRAP cross-chain swap** and then OFT-bridging it to Arbitrum for Boros deposits.

## Withdrawals are two-step (cooldown + finalize)

Boros withdrawals are not always “instant”:
- `withdraw_collateral(...)` requests a withdrawal.
- You may need to wait out a **cooldown** and then call `finalize_vault_withdrawal(...)` to actually receive tokens.

Operationally, treat any pending withdrawal as “capital in flight”:
- Use `get_pending_withdrawal_amount()` / `get_withdrawal_status()` to detect it.
- Avoid placing new hedges or redeploying capital until the withdrawal is finalized.

Reference (MarketHub withdrawal status + cooldown mechanics):
- https://docs.pendle.finance/boros-dev/Contracts/MarketHub

## Safety rails you must apply

- Ensure `web3_service` and `user_address` are configured before expecting broadcasts.
- Treat any calldata returned by Boros API as untrusted input:
  - validate chain id
  - validate `to` address
  - validate the token/amount semantics you intended

## Min cash + isolated/cross gotcha (read this once)

- Some Boros writes require a minimum amount of **cross cash** (see `get_cash_fee_data`).
- Deposits can sometimes credit **isolated** cash for the market; sweep isolated → cross before trading.
