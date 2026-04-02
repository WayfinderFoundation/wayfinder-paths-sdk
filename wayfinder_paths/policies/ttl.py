import time

TTL_DURATION_SECONDS = 300


def build_ttl_policy(ttl_seconds: int = TTL_DURATION_SECONDS) -> list[dict]:
    return [
        {
            "name": "TTL",
            "method": "*",
            "action": "ALLOW",
            "conditions": [
                {
                    "field_source": "system",
                    "field": "current_unix_timestamp",
                    "operator": "lt",
                    "value": str(int(time.time()) + ttl_seconds),
                }
            ],
        }
    ]
