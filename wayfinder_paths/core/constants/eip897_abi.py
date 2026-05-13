"""EIP-897 (DelegateProxy) ABI used to introspect upgradeable proxy contracts."""

from typing import Any

EIP897_ABI: list[dict[str, Any]] = [
    {
        "constant": True,
        "inputs": [],
        "name": "proxyType",
        "outputs": [{"name": "proxyTypeId", "type": "uint256"}],
        "payable": False,
        "stateMutability": "pure",
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "implementation",
        "outputs": [{"name": "codeAddr", "type": "address"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function",
    },
]
