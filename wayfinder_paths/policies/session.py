import time

SESSION_DURATION_SECONDS = 3600


def build_session_policy(duration_seconds: int = SESSION_DURATION_SECONDS) -> dict:
    return {
        "name": "Session",
        "method": "*",
        "action": "ALLOW",
        "conditions": [
            {
                "field_source": "system",
                "field": "current_unix_timestamp",
                "operator": "lt",
                "value": str(int(time.time()) + duration_seconds),
            }
        ],
    }
