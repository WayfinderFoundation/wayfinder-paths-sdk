# Hyperliquid deposits + withdrawals (Bridge2)

This repo uses Hyperliquid’s **Bridge2** deposit/withdraw flow and assumes **Arbitrum (chain_id = 42161)** as the EVM side.

**TL;DR:** To deposit to Hyperliquid, you send **native USDC on Arbitrum** to the Hyperliquid Bridge2 address. Do **not** send USDC from other chains or other assets.

Primary reference:
- Hyperliquid docs: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/bridge2
- Funding cadence (hourly): https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding

## What you can deposit/withdraw

- **Deposit asset:** native **USDC on Arbitrum**
  - This repo’s constant: `ARBITRUM_USDC_ADDRESS = 0xaf88d065e77c8cC2239327C5EDb3A432268e5831`
- **Deposit target:** Bridge2 address on Arbitrum
  - This repo’s constant: `HYPERLIQUID_BRIDGE_ADDRESS = 0x2Df1c51E09aECF9cacB7bc98cB1742757f163dF7`

## Minimums, fees + timing (operational expectations)

From Hyperliquid's Bridge2 docs:
- **Minimum deposit is 5 USDC**; deposits below that are **lost**.
- Deposits are typically credited **in < 1 minute**.
- Withdrawals typically arrive **in several minutes** (often longer than deposits).
- **Withdrawal fee is $1 USDC** — deducted from the withdrawn amount (e.g., withdraw $6.93 → receive $5.93).

Treat these as *best-effort expectations*, not guarantees. In orchestration code, always:
- poll for confirmation
- time out safely
- avoid taking downstream risk (hedges/allocations) until funds are confirmed

## Who gets credited (common pitfall)

Baseline Bridge2 deposit behavior:
- **The Hyperliquid account credited is the sender** of the Arbitrum USDC transfer to the bridge address.

Bridge2 also supports “deposit on behalf” via a permit flow (`batchedDepositWithPermit`) per the docs, but this repo’s strategy patterns assume the simple “send USDC to bridge” path.

## How to monitor deposits/withdrawals in this repo

Adapter: `wayfinder_paths/adapters/hyperliquid_adapter/adapter.py`

### Deposit initiation (hard-coded)

Claude Code shortcut:
- Use `mcp__wayfinder__core_execute(kind="hyperliquid_deposit", wallet_label="main", amount="8")`

This hard-codes:
- token: native Arbitrum USDC (`usd-coin-arbitrum`)
- recipient: `HYPERLIQUID_BRIDGE_ADDRESS`
- chain: Arbitrum (42161)

### Withdrawal initiation

- Call: `HyperliquidAdapter.withdraw(amount, address)` (USDC withdraw to Arbitrum via executor)

Claude Code shortcut:
- Use `mcp__wayfinder__hyperliquid_withdraw(wallet_label=..., amount_usdc=...)`

### Deposit monitoring (recommended)

- Call: `HyperliquidAdapter.wait_for_deposit(address, expected_increase, timeout_s=..., poll_interval_s=...)`
- Mechanism: confirms via the user's non-funding ledger updates (`get_user_deposits`) first; falls back to polling `get_user_state(address)` perp `marginSummary.accountValue`.
- The `mcp__wayfinder__hyperliquid_deposit` shortcut already waits for the credit before returning.

**Under UnifiedAccount mode (the repo default — see [gotchas.md](gotchas.md)):**
- Funds land in the **unified balance**, surfaced via `spotClearinghouseState` as the `USDC` coin (token=0). Perp `marginSummary.accountValue` stays `0` and is not meaningful — per HL docs, individual perp dex user states are not meaningful for unified-account users.
- Confirmation still works correctly because the ledger fast-path runs first.
- The `final_balance_usd` returned by `mcp__wayfinder__hyperliquid_deposit` reads perp margin, so it will report `0` even on a successful deposit. To read the actual credited balance, use `hyperliquid_get_state` and look at `spot.balances[coin="USDC"].total`.

### Withdrawal monitoring (best-effort)

- Call: `HyperliquidAdapter.wait_for_withdrawal(address, max_poll_time_s=..., poll_interval_s=...)`
- Mechanism: polls Hyperliquid ledger updates for a `withdraw` record.

If you need strict “arrived on Arbitrum” confirmation, add an Arbitrum-side receipt check (RPC/Explorer) for the resulting tx hash.

## Orchestration tips

- **Hyperliquid funding is paid hourly**; if you’re rate-locking funding with Boros, align your observations to this cadence.
- Prefer explicit “funding stages” in strategies:
  1) deposit to Hyperliquid
  2) wait for credit
  3) open/adjust hedge
  4) only then deploy spot/yield legs
