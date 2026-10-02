"""Safe exception text at RPC error boundaries, never whole tool payloads."""

import re

_ENDPOINT = re.compile(r"(?:https?|wss?)://[^\s\"'<>]+", re.IGNORECASE)


def safe_rpc_error(error: object) -> str:
    return _ENDPOINT.sub("[RPC endpoint]", str(error))
