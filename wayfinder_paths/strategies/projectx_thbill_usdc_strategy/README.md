# ProjectX THBILL/USDC Strategy

Concentrated-liquidity market making on ProjectX (HyperEVM) for the THBILL/USDC stable pair.

- Pulls USDC (HyperEVM) from `main` wallet into the strategy wallet
- Swaps to the optimal THBILL/USDC split
- Mints or adds liquidity to a tight band around the current tick
- Periodically collects fees, compounds them back into liquidity, and recenters if out of range
- Surfaces ProjectX/Theo points (when available) in `status`
