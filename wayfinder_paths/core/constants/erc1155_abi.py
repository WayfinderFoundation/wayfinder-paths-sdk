from __future__ import annotations

from typing import Any

ERC1155_APPROVAL_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "stateMutability": "view",
        "name": "isApprovedForAll",
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "operator", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "setApprovalForAll",
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "approved", "type": "bool"},
        ],
        "outputs": [],
    },
]
