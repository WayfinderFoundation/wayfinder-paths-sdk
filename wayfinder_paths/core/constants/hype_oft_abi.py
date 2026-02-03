"""LayerZero OFT ABI subset used for bridging native HYPE from HyperEVM."""

HYPE_OFT_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "uint32", "name": "dstEid", "type": "uint32"},
                    {"internalType": "bytes32", "name": "to", "type": "bytes32"},
                    {"internalType": "uint256", "name": "amountLD", "type": "uint256"},
                    {
                        "internalType": "uint256",
                        "name": "minAmountLD",
                        "type": "uint256",
                    },
                    {
                        "internalType": "bytes",
                        "name": "extraOptions",
                        "type": "bytes",
                    },
                    {"internalType": "bytes", "name": "composeMsg", "type": "bytes"},
                    {"internalType": "bytes", "name": "oftCmd", "type": "bytes"},
                ],
                "internalType": "struct SendParam",
                "name": "_sendParam",
                "type": "tuple",
            },
            {"internalType": "bool", "name": "_payInLzToken", "type": "bool"},
        ],
        "name": "quoteSend",
        "outputs": [
            {
                "components": [
                    {"internalType": "uint256", "name": "nativeFee", "type": "uint256"},
                    {
                        "internalType": "uint256",
                        "name": "lzTokenFee",
                        "type": "uint256",
                    },
                ],
                "internalType": "struct MessagingFee",
                "name": "",
                "type": "tuple",
            }
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "uint32", "name": "dstEid", "type": "uint32"},
                    {"internalType": "bytes32", "name": "to", "type": "bytes32"},
                    {"internalType": "uint256", "name": "amountLD", "type": "uint256"},
                    {
                        "internalType": "uint256",
                        "name": "minAmountLD",
                        "type": "uint256",
                    },
                    {
                        "internalType": "bytes",
                        "name": "extraOptions",
                        "type": "bytes",
                    },
                    {"internalType": "bytes", "name": "composeMsg", "type": "bytes"},
                    {"internalType": "bytes", "name": "oftCmd", "type": "bytes"},
                ],
                "internalType": "struct SendParam",
                "name": "_sendParam",
                "type": "tuple",
            },
            {
                "components": [
                    {"internalType": "uint256", "name": "nativeFee", "type": "uint256"},
                    {
                        "internalType": "uint256",
                        "name": "lzTokenFee",
                        "type": "uint256",
                    },
                ],
                "internalType": "struct MessagingFee",
                "name": "_fee",
                "type": "tuple",
            },
            {"internalType": "address", "name": "_refundAddress", "type": "address"},
        ],
        "name": "send",
        "outputs": [
            {
                "components": [
                    {"internalType": "bytes32", "name": "guid", "type": "bytes32"},
                    {"internalType": "uint64", "name": "nonce", "type": "uint64"},
                    {
                        "components": [
                            {
                                "internalType": "uint256",
                                "name": "nativeFee",
                                "type": "uint256",
                            },
                            {
                                "internalType": "uint256",
                                "name": "lzTokenFee",
                                "type": "uint256",
                            },
                        ],
                        "internalType": "struct MessagingFee",
                        "name": "fee",
                        "type": "tuple",
                    },
                ],
                "internalType": "struct MessagingReceipt",
                "name": "",
                "type": "tuple",
            },
            {
                "components": [
                    {
                        "internalType": "uint256",
                        "name": "amountSentLD",
                        "type": "uint256",
                    },
                    {
                        "internalType": "uint256",
                        "name": "amountReceivedLD",
                        "type": "uint256",
                    },
                ],
                "internalType": "struct OFTReceipt",
                "name": "",
                "type": "tuple",
            },
        ],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "sharedDecimals",
        "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimalConversionRate",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]
