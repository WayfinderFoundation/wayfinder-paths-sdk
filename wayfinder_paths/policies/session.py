import time

SESSION_DURATION_SECONDS = 3600
STRATEGY_DURATION_SECONDS = 7 * 24 * 3600


def _ttl_policy(name: str, duration_seconds: int) -> dict:
    return {
        "name": name,
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


def build_session_policy(duration_seconds: int = SESSION_DURATION_SECONDS) -> dict:
    return _ttl_policy("Session", duration_seconds)


def build_strategy_policy(duration_seconds: int = STRATEGY_DURATION_SECONDS) -> dict:
    return _ttl_policy("Strategy", duration_seconds)
