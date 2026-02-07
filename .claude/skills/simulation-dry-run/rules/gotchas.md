# Fork-mode gotchas (Gorlami)

## Flaky RPC (502/503/504)

Fork RPCs can intermittently return 5xx errors. Retry is best-effort; keep scripts resilient:
- retry read calls when safe
- make state assertions after writes

## `eth_estimateGas` failures

Complex router/multicall transactions may fail gas estimation on forks.
- Prefer a safe fallback gas limit in simulation mode.

## Confirmations on forks

Forks often won’t produce additional blocks. Waiting for “3 confirmations” can hang.
- Use 0 confirmations in fork mode (still wait for the receipt).

## Make sure you’re on the fork

Before sending transactions:
- verify the RPC URL points at `.../fork/<id>`
- require `--confirm-live` for real RPC usage

