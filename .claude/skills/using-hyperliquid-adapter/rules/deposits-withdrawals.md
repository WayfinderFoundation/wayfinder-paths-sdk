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
- **Withdrawal fee is $1 USDC** — `mcp__wayfinder__hyperliquid_withdraw_usdc(amount_usdc=N)` debits `$N` from unified; Bridge2 takes $1 out of that, so Arbitrum receives `$N − 1`. Minimum `amount_usdc` is `$2`.

Treat these as _best-effort expectations_, not guarantees. In orchestration code, always:

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

- Use `mcp__wayfinder__hyperliquid_deposit_usdc(wallet_label="main", amount_usdc=8)`

This hard-codes:

- token: native Arbitrum USDC (`usd-coin-arbitrum`)
- recipient: `HYPERLIQUID_BRIDGE_ADDRESS`
- chain: Arbitrum (42161)

### Withdrawal initiation

- Call: `HyperliquidAdapter.withdraw(amount, address)` (USDC withdraw to Arbitrum via executor)

Claude Code shortcut:

- Use `mcp__wayfinder__hyperliquid_withdraw_usdc(wallet_label=..., amount_usdc=...)`

### Deposit monitoring (recommended)

- Call: `HyperliquidAdapter.wait_for_deposit(address, expected_increase, timeout_s=..., poll_interval_s=...)`
- Mechanism: polls **both** balance surfaces — spot USDC (`spotClearinghouseState`, where credits land for unified-account users) and core-dex perp `marginSummary.accountValue` (`clearinghouseState`, where Bridge2 credits land for accounts still in `"default"` split mode, i.e. every fresh account) — and confirms once their sum rises by ≥ 95% of the expected amount. There is no ledger fast-path.
- The `mcp__wayfinder__hyperliquid_deposit_usdc` shortcut waits for the credit, then auto-enables **UnifiedAccount mode** (`ensure_unified` effect) so the balance is withdrawable and shared across spot/perps.
- Tool statuses: `confirmed` (tx + credit observed), `unconfirmed` (Arbitrum tx succeeded but credit not observed within the wait window — funds are likely still in flight; check `hyperliquid_get_state` before retrying, a retry sends additional funds), `failed` (the bridge tx itself failed).
- `final_balance_usd` is the post-credit spot+perp USDC sum.

### Withdrawal monitoring (best-effort)

- Call: `HyperliquidAdapter.wait_for_withdrawal(address, max_poll_time_s=..., poll_interval_s=...)`
- Mechanism: polls Hyperliquid ledger updates for a `withdraw` record.

If you need strict “arrived on Arbitrum” confirmation, add an Arbitrum-side receipt check (RPC/Explorer) for the resulting tx hash.

## Orchestration tips

- **Hyperliquid funding is paid hourly**; if you’re rate-locking funding with Boros, align your observations to this cadence.
- Prefer explicit “funding stages” in strategies:
  1. deposit to Hyperliquid
  2. wait for credit
  3. open/adjust hedge
  4. only then deploy spot/yield legs
