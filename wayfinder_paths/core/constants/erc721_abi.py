from __future__ import annotations

from typing import Any

ERC721_TRANSFER_EVENT_ABI: dict[str, Any] = {
    "type": "event",
    "name": "Transfer",
    "inputs": [
        {"indexed": True, "name": "from", "type": "address"},
        {"indexed": True, "name": "to", "type": "address"},
        {"indexed": True, "name": "tokenId", "type": "uint256"},
    ],
    "anonymous": False,
}
