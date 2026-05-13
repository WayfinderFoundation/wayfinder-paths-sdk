# Polymarket deposit wallet (V2 trading)

Polymarket V2 trades **from a per-user smart contract wallet** ("deposit wallet"), not from the owner EOA. The owner EOA only signs; the deposit wallet holds collateral and shares and is the address that interacts with the CLOB and ConditionalTokens.

**TL;DR:** Before placing your first order, fund the deposit wallet with **pUSD on Polygon**. The owner EOA's pUSD balance is never debited by orders.

## How the deposit wallet works

- **Per-user, CREATE2-derived** from the owner EOA — deterministic, predictable address.
  - `adapter.deposit_wallet_address()` returns it.
- **Auto-deployed on first trade** via `ensure_trading_setup(...)`:
  - Calls the deposit-wallet factory (relayer-mediated)
  - Sets pUSD ERC20 allowance + ConditionalTokens ERC1155 `setApprovalForAll` on the three exchange addresses, all in one signed relayer batch
  - Caches `_setup_complete=True` so subsequent orders skip the readiness check
- **EIP-1271 signer**: orders are typed-data-signed by the owner EOA (POLY_1271 signature type), validated on-chain by the deposit wallet contract via `isValidSignature`.
- **Holds your positions**: ERC1155 conditional shares live at the deposit wallet, not the owner EOA. Same for pUSD collateral used for trading.

## Relayer (sponsored execution)

The deposit wallet is a smart contract — no private key — so every state change on it needs *some* EOA to broadcast the tx and pay Polygon gas. Polymarket runs a **sponsored-tx relayer** at `relayer-v2.polymarket.com` that does this on the user's behalf, same pattern as ERC-4337 bundlers / Gelato / Biconomy. The user signs an EIP-712 `Batch` typed-data message off-chain; the relayer submits it on-chain and pays POL. The deposit wallet contract verifies the signature on-chain before executing — relayer can't forge or modify anything.

**Gas-payer matrix:**

| Action | Gas paid by | Mechanism |
| --- | --- | --- |
| Deposit wallet deploy (first `ensure_trading_setup`) | Relayer | `POST /submit` type=`WALLET-CREATE` |
| Approvals batch (pUSD + CTF × 3 exchanges) | Relayer | `POST /submit` type=`WALLET`, owner signs Batch |
| `withdraw_deposit_wallet` | Relayer | `POST /submit` type=`WALLET`, owner signs Batch |
| `redeem_positions` (+ NegRisk unwrap) | Relayer | Same — owner signs, relayer broadcasts |
| `fund_deposit_wallet` | **Owner EOA** | Direct `pUSD.transfer` from owner; needs POL |
| `bridge_deposit` / `bridge_withdraw` (collateral conversion) | **Owner EOA** | wrap/unwrap + BRAP swaps; needs POL |
| Order placement (CLOB market/limit) | Polymarket CLOB engine | Order signed POLY_1271, matched off-chain, settled on-chain by Polymarket |

Net: the owner EOA needs Polygon POL **only for `fund_deposit_wallet` and the upstream collateral-prep flows**. Everything that touches the deposit wallet contract itself is free.

**Liveness dependency:** withdraws, redemptions, and approval refreshes all require the Polymarket relayer to be up. The adapter has no escape-hatch path that bypasses the relayer — if it goes down, those operations block until it recovers.

## Two-step funding model (the common pitfall)

Trading collateral lives in **two places**, and the adapter has **two distinct flows**:

| Flow | From → To | Asset | Method |
| --- | --- | --- | --- |
| **Collateral preparation** | Polygon USDC/USDC.e ↔ pUSD on **owner EOA** | USDC ↔ pUSD | `bridge_deposit` / `bridge_withdraw` (see `rules/deposits-withdrawals.md`) |
| **Deposit wallet funding** | owner EOA pUSD ↔ deposit wallet pUSD | pUSD only | `fund_deposit_wallet` / `withdraw_deposit_wallet` |

A full first-time flow looks like:
1. `bridge_deposit` — Polygon USDC → pUSD (on owner EOA)
2. `fund_deposit_wallet` — pUSD owner EOA → pUSD deposit wallet
3. trade — `buy` / `sell` / `place_limit_order` / `close_position`
4. (optional) `withdraw_deposit_wallet` — pUSD deposit wallet → pUSD owner EOA
5. (optional) `bridge_withdraw` — pUSD → Polygon USDC

## Operational expectations

- `fund_deposit_wallet` is a **direct ERC20 transfer** on Polygon — confirms in seconds, costs Polygon gas (POL) on the **owner EOA**.
- `withdraw_deposit_wallet` is a **relayer-mediated batch call** (the deposit wallet executes `pUSD.transfer(owner, amount)` against itself via a typed-data-signed batch). Settles in seconds; **the owner EOA pays no gas** since the relayer covers it.
- `ensure_trading_setup` is **cached after the first call** within an adapter instance. The first call does ~8 reads + up to 2 relayer txs (deploy + approval batch); subsequent calls short-circuit.

## MCP shortcuts (Claude Code)

- Fund the deposit wallet: `mcp__wayfinder__polymarket_execute(action="fund_deposit_wallet", wallet_label="main", amount=10)`
- Withdraw from the deposit wallet (omit `amount` to drain): `mcp__wayfinder__polymarket_execute(action="withdraw_deposit_wallet", wallet_label="main", amount=5)`
- Inspect deposit wallet + balances: `mcp__wayfinder__polymarket_get_state(wallet_label="main")` (`deposit_wallet` is the trading address used by all positions/orders)

## Adapter methods

- `adapter.deposit_wallet_address()` — derived address (cheap, no RPC)
- `adapter.fund_deposit_wallet(amount_raw=int)` — pUSD owner → deposit wallet. **`amount_raw` is in base units (6 decimals).** Returns `(ok, {"deposit_wallet", "amount_raw", "tx_hash"})`.
- `adapter.withdraw_deposit_wallet(amount_raw=int | None)` — pUSD deposit wallet → owner. `None` drains the full balance. Returns `(ok, {"deposit_wallet", "tx_hash", "amount_raw", "recipient"})`.
- `adapter.ensure_trading_setup(token_id=...)` — idempotent (cached); deploy + approvals + CLOB creds + balance allowance. Order placement calls this automatically.

## Common pitfalls

- **Funding the wrong address**: shares and CLOB balances live at the deposit wallet, not the owner EOA. `get_full_user_state(...)` reads from the deposit wallet — if you query the owner EOA directly you'll see nothing.
- **Funded too little**: BUY orders fail at the CLOB if the deposit wallet's pUSD is below the order's collateral cost. Fund slightly above your intended trade size (the adapter does no auto-buffer).
- **Stranded dust**: SELL orders need ≥ 0.01 share lots; sub-quantum positions stay on the deposit wallet until `withdraw_deposit_wallet` pulls them — but pUSD-only, the conditional shares stay until you trade or redeem them.
- **Two different `tx_hash` semantics**: `fund_deposit_wallet`'s tx is a direct Polygon ERC20 transfer (find it on Polygonscan under the **owner EOA's** outbound history). `withdraw_deposit_wallet`'s tx is the relayer-submitted batch (find it under the **deposit wallet's** outbound history).
