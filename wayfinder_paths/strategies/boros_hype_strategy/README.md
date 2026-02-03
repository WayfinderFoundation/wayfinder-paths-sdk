# Boros HYPE Strategy

Multi-leg HYPE yield strategy across Boros + HyperEVM + Hyperliquid.

- **Module**: `wayfinder_paths.strategies.boros_hype_strategy.strategy.BorosHypeStrategy`
- **Chains**: Arbitrum (42161), HyperEVM (999), Hyperliquid
- **Collateral token (Arbitrum)**: LayerZero OFT HYPE at `0x007C26Ed5C33Fe6fEF62223d4c363A01F1b1dDc1`

## Funding / Entry (strategy vs ad-hoc)

To deposit HYPE collateral into Boros you need **Arbitrum OFT HYPE**.

- **Strategy entry (cost-min / delta-neutral)**: this strategy typically buys HYPE on Hyperliquid spot, withdraws to HyperEVM, then bridges HyperEVM native HYPE → Arbitrum OFT HYPE for Boros collateral.
- **Ad-hoc Boros funding (preferred)**: if you’re *not* running the full strategy, you can skip Hyperliquid by using a BRAP cross-chain swap to acquire **HyperEVM native HYPE**, then OFT-bridge to Arbitrum for deposit.

## Withdrawal / Exit Gotchas (important)

1. **Boros withdraw delivers OFT HYPE on Arbitrum**
   - There may be **no DEX liquidity** for the OFT token on Arbitrum.
   - The unwind path is: **Arbitrum OFT HYPE → (LayerZero) HyperEVM native HYPE → Hyperliquid spot → sell to USDC**.

2. **Avoid float → int rounding for Boros withdrawal amounts**
   - Gas estimation will fail if the simulated call reverts.
   - Converting a float balance to wei can round **up** by a few wei and trigger a revert.
   - Use Boros-provided integer balances (`cross_wei` / `balance_wei`) when building withdraw amounts.

3. **LayerZero bridge fees are paid in native gas (ETH on Arbitrum)**
   - Arbitrum → HyperEVM bridging of OFT HYPE requires `msg.value = nativeFee` (not the token amount).
   - Make sure the strategy wallet has enough ETH on Arbitrum for the LayerZero fee.

4. **Withdrawals can take time**
   - Boros withdrawals can take ~15 minutes depending on user cooldown and message delivery.
   - If `withdraw()` times out, it is safe to re-run `withdraw()` to continue from the current state.

## Operational Gotchas

1. **Hyperliquid “free margin” (withdrawable) can hit zero**
   - Some steps move USDC from HL perp → HL spot and simultaneously open a matching HYPE perp short.
   - If you move too much USDC out of perp, you may be unable to increase the short later to restore delta neutrality.
   - The strategy caps perp→spot transfers based on HL withdrawable, configured leverage, and a small safety buffer.

2. **Paired fills can partially fill (spot ≠ perp)**
   - If the spot leg fills but the perp leg doesn’t, you can end a tick net long HYPE.
   - The strategy uses slightly higher slippage tolerance for HYPE paired fills and attempts a follow-up repair trade.
   - If hedging still fails due to margin constraints, the strategy trims spot (sells some spot to add margin) and retries.

3. **Spot can be held as WHYPE**
   - Some routes yield WHYPE instead of native HYPE.
   - To send HYPE to Hyperliquid or use it as gas, it must be unwrapped first.

## Actions

```bash
# Status
poetry run python -m wayfinder_paths.run_strategy boros_hype_strategy --action status --config config.json

# Withdraw / unwind to USDC on Arbitrum
poetry run python -m wayfinder_paths.run_strategy boros_hype_strategy --action withdraw --config config.json --debug

# Exit (transfer USDC from strategy wallet back to main wallet)
poetry run python -m wayfinder_paths.run_strategy boros_hype_strategy --action exit --config config.json
```
