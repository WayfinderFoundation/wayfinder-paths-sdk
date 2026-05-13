"""ABIs for the HYPE token ecosystem on HyperEVM:
- WHYPE (wrapped HYPE, WETH-like interface)
- kHYPE staking accountant (exchange rate oracle)
- lHYPE looping accountant (exchange rate oracle)
"""

WHYPE_ABI = [
    {
        "name": "withdraw",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "wad", "type": "uint256"}],
        "outputs": [],
    },
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "owner", "type": "address"}],
        "outputs": [{"type": "uint256"}],
    },
]

KHYPE_STAKING_ACCOUNTANT_ABI = [
    {
        "inputs": [
            {"internalType": "uint256", "name": "kHYPEAmount", "type": "uint256"}
        ],
        "name": "kHYPEToHYPE",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]

LHYPE_ACCOUNTANT_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "quote", "type": "address"}],
        "name": "getRateInQuote",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]
