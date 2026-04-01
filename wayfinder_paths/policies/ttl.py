from __future__ import annotations

import time
from typing import Any


def allow_all_until(
    unix_timestamp: int,
    *,
    chain_type: str = "ethereum",
) -> dict[str, Any]:
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


def allow_all_for(
    seconds_to_live: int,
    *,
    chain_type: str = "ethereum",
) -> dict[str, Any]:
    unix_timestamp = int(time.time()) + seconds_to_live
    return allow_all_until(unix_timestamp, chain_type=chain_type)
