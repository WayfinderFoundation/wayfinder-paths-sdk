# Lido Adapter

Adapter for Lido Ethereum liquid staking (stETH / wstETH) and the async WithdrawalQueue.

## Capabilities

- `staking.stake`: Stake ETH for stETH or wstETH
- `staking.wrap`: Wrap stETH -> wstETH
- `staking.unwrap`: Unwrap wstETH -> stETH
- `withdrawal.request`: Request async withdrawals (stETH or wstETH)
- `withdrawal.claim`: Claim finalized withdrawals (ETH)
- `position.read`: Read balances and withdrawal queue state

