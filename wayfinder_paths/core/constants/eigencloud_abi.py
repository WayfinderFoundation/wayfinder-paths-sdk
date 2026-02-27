"""Minimal ABIs for EigenCloud (EigenLayer) restaking on Ethereum mainnet.

These are intentionally small subsets of the full EigenLayer interfaces, focused on:
- deposits into strategies (StrategyManager)
- delegation + withdrawal queue flows (DelegationManager)
- basic strategy share accounting helpers (IStrategy)
- rewards claiming via merkle proofs (RewardsCoordinator)
"""

ISTRATEGY_MANAGER_ABI = [
    {
        "type": "function",
        "name": "depositIntoStrategy",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "strategy", "type": "address"},
            {"name": "token", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "depositShares", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "depositIntoStrategyWithSignature",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "strategy", "type": "address"},
            {"name": "token", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "staker", "type": "address"},
            {"name": "expiry", "type": "uint256"},
            {"name": "signature", "type": "bytes"},
        ],
        "outputs": [{"name": "depositShares", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "strategyIsWhitelistedForDeposit",
        "stateMutability": "view",
        "inputs": [{"name": "strategy", "type": "address"}],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "type": "function",
        "name": "getDeposits",
        "stateMutability": "view",
        "inputs": [{"name": "staker", "type": "address"}],
        "outputs": [
            {"name": "strategies", "type": "address[]"},
            {"name": "shares", "type": "uint256[]"},
        ],
    },
    {
        "type": "function",
        "name": "stakerDepositShares",
        "stateMutability": "view",
        "inputs": [
            {"name": "user", "type": "address"},
            {"name": "strategy", "type": "address"},
        ],
        "outputs": [{"name": "shares", "type": "uint256"}],
    },
]

IDELEGATION_MANAGER_ABI = [
    {
        "type": "function",
        "name": "delegateTo",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "operator", "type": "address"},
            {
                "name": "approverSignatureAndExpiry",
                "type": "tuple",
                "components": [
                    {"name": "signature", "type": "bytes"},
                    {"name": "expiry", "type": "uint256"},
                ],
            },
            {"name": "approverSalt", "type": "bytes32"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "undelegate",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "staker", "type": "address"}],
        "outputs": [{"name": "withdrawalRoots", "type": "bytes32[]"}],
    },
    {
        "type": "function",
        "name": "redelegate",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "newOperator", "type": "address"},
            {
                "name": "newOperatorApproverSig",
                "type": "tuple",
                "components": [
                    {"name": "signature", "type": "bytes"},
                    {"name": "expiry", "type": "uint256"},
                ],
            },
            {"name": "approverSalt", "type": "bytes32"},
        ],
        "outputs": [{"name": "withdrawalRoots", "type": "bytes32[]"}],
    },
    {
        "type": "function",
        "name": "queueWithdrawals",
        "stateMutability": "nonpayable",
        "inputs": [
            {
                "name": "params",
                "type": "tuple[]",
                "components": [
                    {"name": "strategies", "type": "address[]"},
                    {"name": "depositShares", "type": "uint256[]"},
                    {"name": "__deprecated_withdrawer", "type": "address"},
                ],
            }
        ],
        "outputs": [{"name": "", "type": "bytes32[]"}],
    },
    {
        "type": "function",
        "name": "completeQueuedWithdrawal",
        "stateMutability": "nonpayable",
        "inputs": [
            {
                "name": "withdrawal",
                "type": "tuple",
                "components": [
                    {"name": "staker", "type": "address"},
                    {"name": "delegatedTo", "type": "address"},
                    {"name": "withdrawer", "type": "address"},
                    {"name": "nonce", "type": "uint256"},
                    {"name": "startBlock", "type": "uint32"},
                    {"name": "strategies", "type": "address[]"},
                    {"name": "scaledShares", "type": "uint256[]"},
                ],
            },
            {"name": "tokens", "type": "address[]"},
            {"name": "receiveAsTokens", "type": "bool"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "getDepositedShares",
        "stateMutability": "view",
        "inputs": [{"name": "staker", "type": "address"}],
        "outputs": [
            {"name": "strategies", "type": "address[]"},
            {"name": "depositShares", "type": "uint256[]"},
        ],
    },
    {
        "type": "function",
        "name": "getWithdrawableShares",
        "stateMutability": "view",
        "inputs": [
            {"name": "staker", "type": "address"},
            {"name": "strategies", "type": "address[]"},
        ],
        "outputs": [
            {"name": "withdrawableShares", "type": "uint256[]"},
            {"name": "depositShares", "type": "uint256[]"},
        ],
    },
    {
        "type": "function",
        "name": "delegatedTo",
        "stateMutability": "view",
        "inputs": [{"name": "staker", "type": "address"}],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "type": "function",
        "name": "isDelegated",
        "stateMutability": "view",
        "inputs": [{"name": "staker", "type": "address"}],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "type": "function",
        "name": "getQueuedWithdrawal",
        "stateMutability": "view",
        "inputs": [{"name": "withdrawalRoot", "type": "bytes32"}],
        "outputs": [
            {
                "name": "withdrawal",
                "type": "tuple",
                "components": [
                    {"name": "staker", "type": "address"},
                    {"name": "delegatedTo", "type": "address"},
                    {"name": "withdrawer", "type": "address"},
                    {"name": "nonce", "type": "uint256"},
                    {"name": "startBlock", "type": "uint32"},
                    {"name": "strategies", "type": "address[]"},
                    {"name": "scaledShares", "type": "uint256[]"},
                ],
            },
            {"name": "shares", "type": "uint256[]"},
        ],
    },
    # Optional helper; not required for core flows, but useful for UX.
    {
        "type": "function",
        "name": "delegationApprover",
        "stateMutability": "view",
        "inputs": [{"name": "operator", "type": "address"}],
        "outputs": [{"name": "", "type": "address"}],
    },
    # Events (useful for recovering withdrawal roots from transaction receipts).
    {
        "type": "event",
        "name": "SlashingWithdrawalQueued",
        "anonymous": False,
        "inputs": [
            {"name": "withdrawalRoot", "type": "bytes32", "indexed": False},
            {
                "name": "withdrawal",
                "type": "tuple",
                "indexed": False,
                "components": [
                    {"name": "staker", "type": "address"},
                    {"name": "delegatedTo", "type": "address"},
                    {"name": "withdrawer", "type": "address"},
                    {"name": "nonce", "type": "uint256"},
                    {"name": "startBlock", "type": "uint32"},
                    {"name": "strategies", "type": "address[]"},
                    {"name": "scaledShares", "type": "uint256[]"},
                ],
            },
            {"name": "sharesToWithdraw", "type": "uint256[]", "indexed": False},
        ],
    },
    {
        "type": "event",
        "name": "SlashingWithdrawalCompleted",
        "anonymous": False,
        "inputs": [
            {"name": "withdrawalRoot", "type": "bytes32", "indexed": False},
        ],
    },
]

ISTRATEGY_ABI = [
    {
        "type": "function",
        "name": "underlyingToken",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "type": "function",
        "name": "sharesToUnderlyingView",
        "stateMutability": "view",
        "inputs": [{"name": "amountShares", "type": "uint256"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "underlyingToSharesView",
        "stateMutability": "view",
        "inputs": [{"name": "amountUnderlying", "type": "uint256"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "totalShares",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

IREWARDS_COORDINATOR_ABI = [
    {
        "type": "function",
        "name": "setClaimerFor",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "claimer", "type": "address"}],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "claimerFor",
        "stateMutability": "view",
        "inputs": [{"name": "earner", "type": "address"}],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "type": "function",
        "name": "checkClaim",
        "stateMutability": "view",
        "inputs": [
            {
                "name": "claim",
                "type": "tuple",
                "components": [
                    {"name": "rootIndex", "type": "uint32"},
                    {"name": "earnerIndex", "type": "uint32"},
                    {"name": "earnerTreeProof", "type": "bytes"},
                    {
                        "name": "earnerLeaf",
                        "type": "tuple",
                        "components": [
                            {"name": "earner", "type": "address"},
                            {"name": "earnerTokenRoot", "type": "bytes32"},
                        ],
                    },
                    {"name": "tokenIndices", "type": "uint32[]"},
                    {"name": "tokenTreeProofs", "type": "bytes[]"},
                    {
                        "name": "tokenLeaves",
                        "type": "tuple[]",
                        "components": [
                            {"name": "token", "type": "address"},
                            {"name": "cumulativeEarnings", "type": "uint256"},
                        ],
                    },
                ],
            }
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "type": "function",
        "name": "processClaim",
        "stateMutability": "nonpayable",
        "inputs": [
            {
                "name": "claim",
                "type": "tuple",
                "components": [
                    {"name": "rootIndex", "type": "uint32"},
                    {"name": "earnerIndex", "type": "uint32"},
                    {"name": "earnerTreeProof", "type": "bytes"},
                    {
                        "name": "earnerLeaf",
                        "type": "tuple",
                        "components": [
                            {"name": "earner", "type": "address"},
                            {"name": "earnerTokenRoot", "type": "bytes32"},
                        ],
                    },
                    {"name": "tokenIndices", "type": "uint32[]"},
                    {"name": "tokenTreeProofs", "type": "bytes[]"},
                    {
                        "name": "tokenLeaves",
                        "type": "tuple[]",
                        "components": [
                            {"name": "token", "type": "address"},
                            {"name": "cumulativeEarnings", "type": "uint256"},
                        ],
                    },
                ],
            },
            {"name": "recipient", "type": "address"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "processClaims",
        "stateMutability": "nonpayable",
        "inputs": [
            {
                "name": "claims",
                "type": "tuple[]",
                "components": [
                    {"name": "rootIndex", "type": "uint32"},
                    {"name": "earnerIndex", "type": "uint32"},
                    {"name": "earnerTreeProof", "type": "bytes"},
                    {
                        "name": "earnerLeaf",
                        "type": "tuple",
                        "components": [
                            {"name": "earner", "type": "address"},
                            {"name": "earnerTokenRoot", "type": "bytes32"},
                        ],
                    },
                    {"name": "tokenIndices", "type": "uint32[]"},
                    {"name": "tokenTreeProofs", "type": "bytes[]"},
                    {
                        "name": "tokenLeaves",
                        "type": "tuple[]",
                        "components": [
                            {"name": "token", "type": "address"},
                            {"name": "cumulativeEarnings", "type": "uint256"},
                        ],
                    },
                ],
            },
            {"name": "recipient", "type": "address"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "cumulativeClaimed",
        "stateMutability": "view",
        "inputs": [
            {"name": "claimer", "type": "address"},
            {"name": "token", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "getCurrentClaimableDistributionRoot",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [
            {
                "name": "",
                "type": "tuple",
                "components": [
                    {"name": "root", "type": "bytes32"},
                    {"name": "rewardsCalculationEndTimestamp", "type": "uint32"},
                    {"name": "activatedAt", "type": "uint32"},
                    {"name": "disabled", "type": "bool"},
                ],
            }
        ],
    },
    {
        "type": "function",
        "name": "getDistributionRootsLength",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "getRootIndexFromHash",
        "stateMutability": "view",
        "inputs": [{"name": "rootHash", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "uint32"}],
    },
]
