from __future__ import annotations

# Minimal Euler v2 (EVK / eVault) ABIs: only the functions this SDK calls.
#
# Source ABIs (full):
# - https://raw.githubusercontent.com/euler-xyz/euler-interfaces/master/abis/EthereumVaultConnector.json
# - https://raw.githubusercontent.com/euler-xyz/euler-interfaces/master/abis/EVault.json
# - https://raw.githubusercontent.com/euler-xyz/euler-interfaces/master/abis/VaultLens.json
# - https://raw.githubusercontent.com/euler-xyz/euler-interfaces/master/abis/AccountLens.json
# - https://raw.githubusercontent.com/euler-xyz/euler-interfaces/master/abis/UtilsLens.json
# - https://raw.githubusercontent.com/euler-xyz/euler-interfaces/master/abis/BasePerspective.json

EVC_ABI = [
    {
        "type": "function",
        "name": "batch",
        "inputs": [
            {
                "name": "items",
                "type": "tuple[]",
                "internalType": "struct IEVC.BatchItem[]",
                "components": [
                    {"name": "targetContract", "type": "address"},
                    {"name": "onBehalfOfAccount", "type": "address"},
                    {"name": "value", "type": "uint256"},
                    {"name": "data", "type": "bytes"},
                ],
            }
        ],
        "outputs": [],
        "stateMutability": "payable",
    },
    {
        "type": "function",
        "name": "batchSimulation",
        "inputs": [
            {
                "name": "items",
                "type": "tuple[]",
                "internalType": "struct IEVC.BatchItem[]",
                "components": [
                    {"name": "targetContract", "type": "address"},
                    {"name": "onBehalfOfAccount", "type": "address"},
                    {"name": "value", "type": "uint256"},
                    {"name": "data", "type": "bytes"},
                ],
            }
        ],
        "outputs": [
            {
                "name": "batchItemsResult",
                "type": "tuple[]",
                "internalType": "struct IEVC.BatchItemResult[]",
                "components": [
                    {"name": "success", "type": "bool"},
                    {"name": "result", "type": "bytes"},
                ],
            },
            {
                "name": "accountsStatusCheckResult",
                "type": "tuple[]",
                "internalType": "struct IEVC.StatusCheckResult[]",
                "components": [
                    {"name": "checkedAddress", "type": "address"},
                    {"name": "isValid", "type": "bool"},
                    {"name": "result", "type": "bytes"},
                ],
            },
            {
                "name": "vaultsStatusCheckResult",
                "type": "tuple[]",
                "internalType": "struct IEVC.StatusCheckResult[]",
                "components": [
                    {"name": "checkedAddress", "type": "address"},
                    {"name": "isValid", "type": "bool"},
                    {"name": "result", "type": "bytes"},
                ],
            },
        ],
        "stateMutability": "payable",
    },
    {
        "type": "function",
        "name": "enableCollateral",
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "vault", "type": "address"},
        ],
        "outputs": [],
        "stateMutability": "payable",
    },
    {
        "type": "function",
        "name": "disableCollateral",
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "vault", "type": "address"},
        ],
        "outputs": [],
        "stateMutability": "payable",
    },
    {
        "type": "function",
        "name": "enableController",
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "vault", "type": "address"},
        ],
        "outputs": [],
        "stateMutability": "payable",
    },
    {
        "type": "function",
        "name": "disableController",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [],
        "stateMutability": "payable",
    },
]

EVAULT_ABI = [
    {
        "type": "function",
        "name": "asset",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "dToken",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "deposit",
        "inputs": [
            {"name": "amount", "type": "uint256"},
            {"name": "receiver", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "nonpayable",
    },
    {
        "type": "function",
        "name": "withdraw",
        "inputs": [
            {"name": "amount", "type": "uint256"},
            {"name": "receiver", "type": "address"},
            {"name": "owner", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "nonpayable",
    },
    {
        "type": "function",
        "name": "redeem",
        "inputs": [
            {"name": "amount", "type": "uint256"},
            {"name": "receiver", "type": "address"},
            {"name": "owner", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "nonpayable",
    },
    {
        "type": "function",
        "name": "borrow",
        "inputs": [
            {"name": "amount", "type": "uint256"},
            {"name": "receiver", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "nonpayable",
    },
    {
        "type": "function",
        "name": "repay",
        "inputs": [
            {"name": "amount", "type": "uint256"},
            {"name": "receiver", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "nonpayable",
    },
    {
        "type": "function",
        "name": "balanceOf",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "debtOf",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "maxWithdraw",
        "inputs": [{"name": "owner", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
]

UTILS_LENS_ABI = [
    {
        "type": "function",
        "name": "getAPYs",
        "inputs": [{"name": "vault", "type": "address"}],
        "outputs": [
            {"name": "borrowAPY", "type": "uint256"},
            {"name": "supplyAPY", "type": "uint256"},
        ],
        "stateMutability": "view",
    }
]

PERSPECTIVE_ABI = [
    {
        "type": "function",
        "name": "verifiedArray",
        "inputs": [],
        "outputs": [{"name": "", "type": "address[]"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "isVerified",
        "inputs": [{"name": "vault", "type": "address"}],
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
    },
]

VAULT_LENS_ABI = [
    {
        "type": "function",
        "name": "getVaultInfoFull",
        "inputs": [{"name": "vault", "type": "address"}],
        "outputs": [
            {
                "name": "",
                "type": "tuple",
                "components": [
                    {"name": "timestamp", "type": "uint256"},
                    {"name": "vault", "type": "address"},
                    {"name": "vaultName", "type": "string"},
                    {"name": "vaultSymbol", "type": "string"},
                    {"name": "vaultDecimals", "type": "uint256"},
                    {"name": "asset", "type": "address"},
                    {"name": "assetName", "type": "string"},
                    {"name": "assetSymbol", "type": "string"},
                    {"name": "assetDecimals", "type": "uint256"},
                    {"name": "unitOfAccount", "type": "address"},
                    {"name": "unitOfAccountName", "type": "string"},
                    {"name": "unitOfAccountSymbol", "type": "string"},
                    {"name": "unitOfAccountDecimals", "type": "uint256"},
                    {"name": "totalShares", "type": "uint256"},
                    {"name": "totalCash", "type": "uint256"},
                    {"name": "totalBorrowed", "type": "uint256"},
                    {"name": "totalAssets", "type": "uint256"},
                    {"name": "accumulatedFeesShares", "type": "uint256"},
                    {"name": "accumulatedFeesAssets", "type": "uint256"},
                    {"name": "governorFeeReceiver", "type": "address"},
                    {"name": "protocolFeeReceiver", "type": "address"},
                    {"name": "protocolFeeShare", "type": "uint256"},
                    {"name": "interestFee", "type": "uint256"},
                    {"name": "hookedOperations", "type": "uint256"},
                    {"name": "configFlags", "type": "uint256"},
                    {"name": "supplyCap", "type": "uint256"},
                    {"name": "borrowCap", "type": "uint256"},
                    {"name": "maxLiquidationDiscount", "type": "uint256"},
                    {"name": "liquidationCoolOffTime", "type": "uint256"},
                    {"name": "dToken", "type": "address"},
                    {"name": "oracle", "type": "address"},
                    {"name": "interestRateModel", "type": "address"},
                    {"name": "hookTarget", "type": "address"},
                    {"name": "evc", "type": "address"},
                    {"name": "protocolConfig", "type": "address"},
                    {"name": "balanceTracker", "type": "address"},
                    {"name": "permit2", "type": "address"},
                    {"name": "creator", "type": "address"},
                    {"name": "governorAdmin", "type": "address"},
                    {
                        "name": "irmInfo",
                        "type": "tuple",
                        "components": [
                            {"name": "queryFailure", "type": "bool"},
                            {"name": "queryFailureReason", "type": "bytes"},
                            {"name": "vault", "type": "address"},
                            {"name": "interestRateModel", "type": "address"},
                            {
                                "name": "interestRateInfo",
                                "type": "tuple[]",
                                "components": [
                                    {"name": "cash", "type": "uint256"},
                                    {"name": "borrows", "type": "uint256"},
                                    {"name": "borrowSPY", "type": "uint256"},
                                    {"name": "borrowAPY", "type": "uint256"},
                                    {"name": "supplyAPY", "type": "uint256"},
                                ],
                            },
                            {
                                "name": "interestRateModelInfo",
                                "type": "tuple",
                                "components": [
                                    {
                                        "name": "interestRateModel",
                                        "type": "address",
                                    },
                                    {
                                        "name": "interestRateModelType",
                                        "type": "uint8",
                                    },
                                    {
                                        "name": "interestRateModelParams",
                                        "type": "bytes",
                                    },
                                ],
                            },
                        ],
                    },
                    {
                        "name": "collateralLTVInfo",
                        "type": "tuple[]",
                        "components": [
                            {"name": "collateral", "type": "address"},
                            {"name": "borrowLTV", "type": "uint256"},
                            {"name": "liquidationLTV", "type": "uint256"},
                            {"name": "initialLiquidationLTV", "type": "uint256"},
                            {"name": "targetTimestamp", "type": "uint256"},
                            {"name": "rampDuration", "type": "uint256"},
                        ],
                    },
                    {
                        "name": "liabilityPriceInfo",
                        "type": "tuple",
                        "components": [
                            {"name": "queryFailure", "type": "bool"},
                            {"name": "queryFailureReason", "type": "bytes"},
                            {"name": "timestamp", "type": "uint256"},
                            {"name": "oracle", "type": "address"},
                            {"name": "asset", "type": "address"},
                            {"name": "unitOfAccount", "type": "address"},
                            {"name": "amountIn", "type": "uint256"},
                            {"name": "amountOutMid", "type": "uint256"},
                            {"name": "amountOutBid", "type": "uint256"},
                            {"name": "amountOutAsk", "type": "uint256"},
                        ],
                    },
                    {
                        "name": "collateralPriceInfo",
                        "type": "tuple[]",
                        "components": [
                            {"name": "queryFailure", "type": "bool"},
                            {"name": "queryFailureReason", "type": "bytes"},
                            {"name": "timestamp", "type": "uint256"},
                            {"name": "oracle", "type": "address"},
                            {"name": "asset", "type": "address"},
                            {"name": "unitOfAccount", "type": "address"},
                            {"name": "amountIn", "type": "uint256"},
                            {"name": "amountOutMid", "type": "uint256"},
                            {"name": "amountOutBid", "type": "uint256"},
                            {"name": "amountOutAsk", "type": "uint256"},
                        ],
                    },
                    {
                        "name": "oracleInfo",
                        "type": "tuple",
                        "components": [
                            {"name": "oracle", "type": "address"},
                            {"name": "name", "type": "string"},
                            {"name": "oracleInfo", "type": "bytes"},
                        ],
                    },
                    {
                        "name": "backupAssetPriceInfo",
                        "type": "tuple",
                        "components": [
                            {"name": "queryFailure", "type": "bool"},
                            {"name": "queryFailureReason", "type": "bytes"},
                            {"name": "timestamp", "type": "uint256"},
                            {"name": "oracle", "type": "address"},
                            {"name": "asset", "type": "address"},
                            {"name": "unitOfAccount", "type": "address"},
                            {"name": "amountIn", "type": "uint256"},
                            {"name": "amountOutMid", "type": "uint256"},
                            {"name": "amountOutBid", "type": "uint256"},
                            {"name": "amountOutAsk", "type": "uint256"},
                        ],
                    },
                    {
                        "name": "backupAssetOracleInfo",
                        "type": "tuple",
                        "components": [
                            {"name": "oracle", "type": "address"},
                            {"name": "name", "type": "string"},
                            {"name": "oracleInfo", "type": "bytes"},
                        ],
                    },
                ],
            }
        ],
        "stateMutability": "view",
    }
]

ACCOUNT_LENS_ABI = [
    {
        "type": "function",
        "name": "getAccountInfo",
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "vault", "type": "address"},
        ],
        "outputs": [
            {
                "name": "",
                "type": "tuple",
                "components": [
                    {
                        "name": "evcAccountInfo",
                        "type": "tuple",
                        "components": [
                            {"name": "timestamp", "type": "uint256"},
                            {"name": "evc", "type": "address"},
                            {"name": "account", "type": "address"},
                            {"name": "addressPrefix", "type": "bytes19"},
                            {"name": "owner", "type": "address"},
                            {"name": "isLockdownMode", "type": "bool"},
                            {"name": "isPermitDisabledMode", "type": "bool"},
                            {
                                "name": "lastAccountStatusCheckTimestamp",
                                "type": "uint256",
                            },
                            {"name": "enabledControllers", "type": "address[]"},
                            {"name": "enabledCollaterals", "type": "address[]"},
                        ],
                    },
                    {
                        "name": "vaultAccountInfo",
                        "type": "tuple",
                        "components": [
                            {"name": "timestamp", "type": "uint256"},
                            {"name": "account", "type": "address"},
                            {"name": "vault", "type": "address"},
                            {"name": "asset", "type": "address"},
                            {"name": "assetsAccount", "type": "uint256"},
                            {"name": "shares", "type": "uint256"},
                            {"name": "assets", "type": "uint256"},
                            {"name": "borrowed", "type": "uint256"},
                            {"name": "assetAllowanceVault", "type": "uint256"},
                            {"name": "assetAllowanceVaultPermit2", "type": "uint256"},
                            {
                                "name": "assetAllowanceExpirationVaultPermit2",
                                "type": "uint256",
                            },
                            {"name": "assetAllowancePermit2", "type": "uint256"},
                            {"name": "balanceForwarderEnabled", "type": "bool"},
                            {"name": "isController", "type": "bool"},
                            {"name": "isCollateral", "type": "bool"},
                            {
                                "name": "liquidityInfo",
                                "type": "tuple",
                                "components": [
                                    {"name": "queryFailure", "type": "bool"},
                                    {"name": "queryFailureReason", "type": "bytes"},
                                    {"name": "account", "type": "address"},
                                    {"name": "vault", "type": "address"},
                                    {"name": "unitOfAccount", "type": "address"},
                                    {"name": "timeToLiquidation", "type": "int256"},
                                    {
                                        "name": "liabilityValueBorrowing",
                                        "type": "uint256",
                                    },
                                    {
                                        "name": "liabilityValueLiquidation",
                                        "type": "uint256",
                                    },
                                    {
                                        "name": "collateralValueBorrowing",
                                        "type": "uint256",
                                    },
                                    {
                                        "name": "collateralValueLiquidation",
                                        "type": "uint256",
                                    },
                                ],
                            },
                        ],
                    },
                    {
                        "name": "accountRewardInfo",
                        "type": "tuple",
                        "components": [
                            {"name": "timestamp", "type": "uint256"},
                            {"name": "account", "type": "address"},
                            {"name": "vault", "type": "address"},
                            {"name": "balanceTracker", "type": "address"},
                            {"name": "balanceForwarderEnabled", "type": "bool"},
                            {"name": "balance", "type": "uint256"},
                            {
                                "name": "enabledRewardsInfo",
                                "type": "tuple[]",
                                "components": [
                                    {"name": "reward", "type": "address"},
                                    {"name": "earnedReward", "type": "uint256"},
                                    {
                                        "name": "earnedRewardRecentIgnored",
                                        "type": "uint256",
                                    },
                                ],
                            },
                        ],
                    },
                ],
            }
        ],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "getAccountEnabledVaultsInfo",
        "inputs": [
            {"name": "evc", "type": "address"},
            {"name": "account", "type": "address"},
        ],
        "outputs": [
            {
                "name": "",
                "type": "tuple",
                "components": [
                    {
                        "name": "evcAccountInfo",
                        "type": "tuple",
                        "components": [
                            {"name": "timestamp", "type": "uint256"},
                            {"name": "evc", "type": "address"},
                            {"name": "account", "type": "address"},
                            {"name": "addressPrefix", "type": "bytes19"},
                            {"name": "owner", "type": "address"},
                            {"name": "isLockdownMode", "type": "bool"},
                            {"name": "isPermitDisabledMode", "type": "bool"},
                            {
                                "name": "lastAccountStatusCheckTimestamp",
                                "type": "uint256",
                            },
                            {"name": "enabledControllers", "type": "address[]"},
                            {"name": "enabledCollaterals", "type": "address[]"},
                        ],
                    },
                    {
                        "name": "vaultAccountInfo",
                        "type": "tuple[]",
                        "components": [
                            {"name": "timestamp", "type": "uint256"},
                            {"name": "account", "type": "address"},
                            {"name": "vault", "type": "address"},
                            {"name": "asset", "type": "address"},
                            {"name": "assetsAccount", "type": "uint256"},
                            {"name": "shares", "type": "uint256"},
                            {"name": "assets", "type": "uint256"},
                            {"name": "borrowed", "type": "uint256"},
                            {"name": "assetAllowanceVault", "type": "uint256"},
                            {"name": "assetAllowanceVaultPermit2", "type": "uint256"},
                            {
                                "name": "assetAllowanceExpirationVaultPermit2",
                                "type": "uint256",
                            },
                            {"name": "assetAllowancePermit2", "type": "uint256"},
                            {"name": "balanceForwarderEnabled", "type": "bool"},
                            {"name": "isController", "type": "bool"},
                            {"name": "isCollateral", "type": "bool"},
                            {
                                "name": "liquidityInfo",
                                "type": "tuple",
                                "components": [
                                    {"name": "queryFailure", "type": "bool"},
                                    {"name": "queryFailureReason", "type": "bytes"},
                                    {"name": "account", "type": "address"},
                                    {"name": "vault", "type": "address"},
                                    {"name": "unitOfAccount", "type": "address"},
                                    {"name": "timeToLiquidation", "type": "int256"},
                                    {
                                        "name": "liabilityValueBorrowing",
                                        "type": "uint256",
                                    },
                                    {
                                        "name": "liabilityValueLiquidation",
                                        "type": "uint256",
                                    },
                                    {
                                        "name": "collateralValueBorrowing",
                                        "type": "uint256",
                                    },
                                    {
                                        "name": "collateralValueLiquidation",
                                        "type": "uint256",
                                    },
                                ],
                            },
                        ],
                    },
                    {
                        "name": "accountRewardInfo",
                        "type": "tuple[]",
                        "components": [
                            {"name": "timestamp", "type": "uint256"},
                            {"name": "account", "type": "address"},
                            {"name": "vault", "type": "address"},
                            {"name": "balanceTracker", "type": "address"},
                            {"name": "balanceForwarderEnabled", "type": "bool"},
                            {"name": "balance", "type": "uint256"},
                            {
                                "name": "enabledRewardsInfo",
                                "type": "tuple[]",
                                "components": [
                                    {"name": "reward", "type": "address"},
                                    {"name": "earnedReward", "type": "uint256"},
                                    {
                                        "name": "earnedRewardRecentIgnored",
                                        "type": "uint256",
                                    },
                                ],
                            },
                        ],
                    },
                ],
            }
        ],
        "stateMutability": "view",
    },
]
