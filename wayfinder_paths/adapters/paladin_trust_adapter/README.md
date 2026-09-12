# Paladin Trust Adapter

Pre-trade token trust gate — a **"trust-check before swap"** companion to the
SDK's *quote-before-swap* pattern. Screens a token contract for honeypot, rug,
scam, unverified-source and sanction risk before an agent routes a swap into it.

- **Type**: `TRUST`
- **Module**: `wayfinder_paths.adapters.paladin_trust_adapter.adapter.PaladinTrustAdapter`

## Overview

The adapter calls [PaladinFi](https://paladinfi.com)'s trust API on Base
(chainId 8453) and returns a single `allow` / `warn` / `block` recommendation
plus the contributing factors. The verdict composes four signal sources:

- **GoPlus** token security (honeypot, hidden owner, reversible renounce, DEX liquidity, trust list)
- **Etherscan** source verification (verified vs unverified contracts, proxy detection)
- **PaladinFi anomaly** heuristics (contract age, no-outbound-history flags)
- **OFAC SDN** screening (refreshed daily from the U.S. Treasury XML feed)

PaladinFi is non-custodial — it never holds funds or signs swaps. The adapter
only reads a risk verdict; the agent keeps full control of execution.

## Two tiers

| Method | Endpoint | Cost | Signer |
|---|---|---|---|
| `check_token` | `POST /v1/trust-check` | x402-paid, $0.001 USDC/call on Base | required |
| `screen_wallet_ofac` | `POST /v1/trust-check/ofac` | free | none |

`check_token` runs the full composition and is the pre-swap gate.
`screen_wallet_ofac` is a free OFAC-only screen, useful for vetting a
recipient/taker wallet without a payment.

### Verdicts

`result["trust"]["recommendation"]` is one of:

- `block` — do not proceed (OFAC SDN hit, or a high-severity GoPlus flag).
- `warn` — proceed only with explicit handling (e.g. a GoPlus honeypot flag, an
  unverified contract, or — for the free OFAC screen — a transiently unreachable
  SDN source, per the API's fail-closed contract).
- `allow` — no risk factor fired.

`result["trust"]["factors"]` lists the contributing `{source, signal, details}`
so the agent can log or branch on the specific reason.

## Usage

```python
from wayfinder_paths.adapters.paladin_trust_adapter.adapter import PaladinTrustAdapter

# Paid full check needs a signer (eth-account LocalAccount or a hex key).
adapter = PaladinTrustAdapter({"private_key": "0x..."})

success, result = await adapter.check_token(
    "0x940181a94A35A4569E4529A3CDfB74e38FD98631",  # AERO on Base
    chain_id=8453,
)
if success and result["trust"]["recommendation"] == "block":
    raise RuntimeError(f"trust gate blocked token: {result['trust']['factors']}")
```

## Methods

### check_token

Full trust composition for a token contract (x402-paid). Requires a signer in
config to authorize the $0.001 USDC micropayment.

```python
success, result = await adapter.check_token(token_address, chain_id=8453)
# result["trust"]["recommendation"] in {"allow", "warn", "block"}
# result["trust"]["factors"] -> list of {source, signal, details}
```

If no signer is configured, returns `(False, "<reason>")` and you should fall
back to `screen_wallet_ofac` or configure a signer.

### screen_wallet_ofac

Free OFAC SDN screen for any wallet or contract address. No payment, no signer.

```python
success, result = await adapter.screen_wallet_ofac(address, chain_id=8453)
# result["trust"]["recommendation"] in {"allow", "block"}
```

## Payment (x402)

`check_token` pays per call over [x402](https://x402.org): on the `402` response
it signs an EIP-3009 `transferWithAuthorization` for USDC on Base with the
configured signer and retries. The signing is assembled with `eth-account`
(already an SDK dependency) — **no additional package is required**.

## Configuration

| Key | Default | Notes |
|---|---|---|
| `signer` | `None` | eth-account `LocalAccount` for the x402 micropayment |
| `private_key` | `None` | hex key used to build a signer if `signer` is absent |
| `base_url` | `https://swap.paladinfi.com` | API host |
| `timeout` | `30` | per-request timeout (seconds) |

## Dependencies

- `httpx`, `eth-account` — both already in `wayfinder-paths`. No new dependency.

## Testing

```bash
poetry run pytest wayfinder_paths/adapters/paladin_trust_adapter/ -v
```
