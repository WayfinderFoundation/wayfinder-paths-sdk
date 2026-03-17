# SparkLend Adapter

SparkLend is an Aave v3-style lending market (Pool + ProtocolDataProvider + RewardsController + WETHGateway).

This adapter supports:
- Supply / withdraw (Pool.supply / Pool.withdraw)
- Borrow / repay (Pool.borrow / Pool.repay)
- Enable / disable collateral (Pool.setUserUseReserveAsCollateral)
- Market discovery & user positions (AaveProtocolDataProvider)
- Rewards claiming (RewardsController.claimAllRewardsToSelf)
- Native helpers on supported chains (WETHGateway)

