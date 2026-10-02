"""Small shared boundary for fixed-origin clients and durable mutation receipts."""

from __future__ import annotations

import fcntl
import hashlib
import json
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
from eth_account import Account
from eth_account.messages import encode_typed_data

from wayfinder_paths.runner.monitor_state import atomic_write_json

TypedSigner = Callable[[dict[str, Any]], Awaitable[str]]
MessageSigner = Callable[[bytes], Awaitable[bytes]]


async def signed_typed_data(
    signer: TypedSigner, address: str, payload: dict[str, Any]
) -> str:
    signature = await signer(payload)
    encoded = encode_typed_data(full_message=payload)
    if Account.recover_message(encoded, signature=signature).lower() != address.lower():
        raise ValueError("typed-data signer identity mismatch")
    return signature


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


@contextmanager
def state_lock(directory: Path) -> Iterator[None]:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "execution.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


class FixedOriginClient:
    def __init__(
        self, origin: str, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.origin = origin
        self.http = httpx.AsyncClient(
            base_url=origin, transport=transport, timeout=10, follow_redirects=False
        )

    async def close(self) -> None:
        await self.http.aclose()

    async def _request(
        self, method: str, path: str, *, allow_list: bool = False, **kwargs: Any
    ) -> Any:
        if not path.startswith("/v1/") or ".." in path or "\\" in path:
            raise ValueError("invalid protocol API path")
        try:
            response = await self.http.request(method, path, **kwargs)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Never surface request headers, signed payloads, or provider bodies.
            raise RuntimeError(
                f"protocol request failed ({type(exc).__name__})"
            ) from None
        if isinstance(body, list) and allow_list:
            return body
        if (
            not isinstance(body, dict)
            or body.get("success") is False
            or body.get("error")
            or body.get("code")
        ):
            raise ValueError("protocol refused request or returned invalid data")
        result = body.get("data", body)
        if not isinstance(result, dict) and not (
            allow_list and isinstance(result, list)
        ):
            raise ValueError("invalid protocol response data")
        return result


class CommandJournal:
    """Caller holds an account-wide state_lock across prepare/send/save.

    A reserved command is never re-sent, including after a process crash. Its
    caller must reconcile through venue order/history reads. No secrets saved.
    """

    def __init__(self, directory: Path, identity: str) -> None:
        self.path = directory / "commands.json"
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
            if self.data["identity"] != identity or not isinstance(
                self.data["commands"], dict
            ):
                raise ValueError("execution identity changed")
        else:
            self.data = {"identity": identity, "commands": {}}

    def reserve(self, key: str, request: Any, **metadata: Any) -> dict[str, Any] | None:
        fingerprint = digest(request)
        prior = self.data["commands"].get(key)
        if prior:
            if prior["fingerprint"] != fingerprint:
                raise ValueError("operation ID reused with different parameters")
            return prior.get("response") or {
                "status": "reconcile_required",
                "operation_id": key,
            }
        self.data["commands"][key] = {"fingerprint": fingerprint, **metadata}
        atomic_write_json(self.path, self.data)
        return None

    def complete(self, key: str, response: dict[str, Any]) -> dict[str, Any]:
        self.data["commands"][key]["response"] = response
        atomic_write_json(self.path, self.data)
        return response
