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

## Backtesting

### Yield sources and data availability

This strategy earns from three components, none of which are fully available in Delta Lab:

| Component | Data source | In Delta Lab? |
|---|---|---|
| kHYPE staking yield | Kinetiq API (`kinetiq.xyz/api/khype`, field `apy_14d`) | No |
| lHYPE looped yield | Looping Collective API (`app.loopingcollective.org`, field `reward_rate`) | No |
| Boros fixed-rate | Boros on-chain markets | No |
| HL HYPE perp funding | Hyperliquid funding history | **Yes** |

A full end-to-end backtest is not possible from Delta Lab data alone. The most meaningful backtestable component is **whether HL HYPE funding was consistently positive** (validating the delta-hedge income).

### HYPE perp funding backtest

```python
from wayfinder_paths.core.backtesting import (
    fetch_funding_rates, fetch_prices, backtest_delta_neutral,
)

# Check historical HYPE funding — this tells you if the perp short was
# collecting funding (total_funding <= 0 means income received)
result = await backtest_delta_neutral(
    ["HYPE"], "2025-08-01", "2026-02-01",
    funding_threshold=0.0,   # enter whenever available (no minimum threshold)
    leverage=float(2),       # MAX_HL_LEVERAGE = 2.0
)
print(f"Funding income:  {result.stats.get('total_funding', 0):.2%}")
print(f"Volatility ann:  {result.stats.get('volatility_ann', 0):.2%}")
print(f"Max drawdown:    {result.stats['max_drawdown']:.2%}")
# Health: total_funding <= 0 (income), volatility_ann low (hedge working)
```

To estimate total strategy APY, add the current kHYPE/lHYPE/Boros APYs on top of the funding income:

```python
# Fetch live HYPE staking APYs for additive estimation (point-in-time only)
import aiohttp

async with aiohttp.ClientSession() as session:
    async with session.get("https://kinetiq.xyz/api/khype") as resp:
        khype_data = await resp.json()
        khype_apy = float(khype_data.get("apy_14d", 0))

    async with session.get("https://app.loopingcollective.org/api/external/asset/lhype") as resp:
        lhype_data = await resp.json()
        lhype_apy = float((lhype_data.get("result") or {}).get("reward_rate", 0)) / 100.0

print(f"kHYPE APY (14d): {khype_apy:.2%}")
print(f"lHYPE APY:       {lhype_apy:.2%}")
# Add the HL funding income from the backtest for a rough total estimate
```

**Key caveat**: The kHYPE/lHYPE APYs are point-in-time only — there is no historical time series available via Delta Lab. The above estimates represent current conditions, not the historical average over the backtest period.

## Actions

```bash
# Status
poetry run python -m wayfinder_paths.run_strategy boros_hype_strategy --action status --config config.json

# Withdraw / unwind to USDC on Arbitrum
poetry run python -m wayfinder_paths.run_strategy boros_hype_strategy --action withdraw --config config.json --debug

# Exit (transfer USDC from strategy wallet back to main wallet)
poetry run python -m wayfinder_paths.run_strategy boros_hype_strategy --action exit --config config.json
```
