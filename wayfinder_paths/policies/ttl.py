from __future__ import annotations

import time
from typing import Any


def allow_all_for(
    seconds_to_live: int,
    *,
    chain_type: str = "ethereum",
) -> dict[str, Any]:
    if seconds_to_live <= 0:
        raise ValueError("seconds_to_live must be a positive integer")

    unix_timestamp = int(time.time()) + seconds_to_live
    return {
        "version": "1.0",
        "name": "TTL",
        "chain_type": chain_type,
        "rules": [
            {
                "name": f"Allow all actions before {unix_timestamp}",
                "method": "*",
                "conditions": [
                    {
                        "field_source": "system",
                        "field": "current_unix_timestamp",
                        "operator": "lt",
                        "value": str(unix_timestamp),
                    }
                ],
                "action": "ALLOW",
            }
        ],
    }
