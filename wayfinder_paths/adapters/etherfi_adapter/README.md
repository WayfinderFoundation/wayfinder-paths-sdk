# ether.fi Adapter

Adapter for ether.fi ETH liquid restaking (eETH / weETH) and async withdrawals via WithdrawRequestNFT.

## Capabilities

- `staking.stake`: Stake ETH -> eETH (shares-based)
- `staking.wrap`: Wrap eETH -> weETH
- `staking.unwrap`: Unwrap weETH -> eETH
- `withdrawal.request`: Request async withdrawal (mints WithdrawRequest NFT)
- `withdrawal.claim`: Claim finalized withdrawal (burns NFT -> receive ETH)
- `position.read`: Read eETH/weETH balances and conversions

