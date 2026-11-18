"""FLOP's hosted gateway (OpenAI-compatible).

Gateway requests are paid from the gateway operator's account, not the user's FLOP account,
so they show the network working but are not purchases by the user's account. Use a direct
session for those.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from wayfinder_paths.core.flop import state
from wayfinder_paths.core.flop.signer import FlopSigner

GATEWAY_URL = "https://api.flop.network/v1"
TIMEOUT_SECONDS = 180
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
OVERLOAD_ATTEMPTS = 3
OVERLOAD_WAIT_SECONDS = 35


class GatewayError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"FLOP gateway {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class GatewayReply:
    text: str
    model: str
    request_id: str
    prompt_tokens: int
    completion_tokens: int
    cost_flop: float | None
    receipt: dict[str, Any] | None


def _body(response: httpx.Response) -> Any:
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise GatewayError(
            response.status_code, "response_too_large", "response too large"
        )
    return response.json()


def _raise(response: httpx.Response) -> None:
    body = _body(response)
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        raise GatewayError(
            response.status_code, str(error.get("code")), str(error.get("message"))
        )
    raise GatewayError(response.status_code, str(error or "error"), str(body)[:200])


async def _request(
    method: str,
    path: str,
    *,
    bearer: str | None = None,
    json: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    request_headers = dict(headers or {})
    if bearer:
        request_headers["Authorization"] = f"Bearer {bearer}"
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        return await client.request(
            method,
            f"{GATEWAY_URL}{path}",
            json=json,
            params=params,
            headers=request_headers,
        )


async def models() -> list[dict[str, Any]]:
    response = await _request("GET", "/models")
    if response.status_code != 200:
        _raise(response)
    return _body(response)["data"]


async def enroll(signer: FlopSigner) -> dict[str, Any]:
    """Exchange a signed challenge for this account's gateway key and store it."""
    challenge = await _request(
        "GET", "/enroll/wallet/challenge", params={"address": signer.address}
    )
    if challenge.status_code != 200:
        _raise(challenge)
    message = _body(challenge)["message"]
    signature = await signer.sign_enrollment(message)
    response = await _request(
        "POST",
        "/enroll/wallet",
        json={
            "address": signer.address,
            "message": message,
            "signature": "0x" + signature.hex(),
        },
    )
    if response.status_code not in (200, 201):
        _raise(response)
    enrollment = _body(response)
    keys = state.read_state().get("gateway", {})
    keys[signer.address] = {
        "key": enrollment["key"],
        "model": enrollment["model"],
        "expires_at": int(enrollment["expires_at"]) // 1000,
    }
    state.update_state(gateway=keys)
    return {k: v for k, v in keys[signer.address].items() if k != "key"}


def stored_key(address: str, *, now: float | None = None) -> dict[str, Any] | None:
    record = state.read_state().get("gateway", {}).get(address)
    current = now if now is not None else time.time()
    if not record or record["expires_at"] <= current:
        return None
    return record


async def ensure_key(signer: FlopSigner) -> dict[str, Any]:
    if stored_key(signer.address) is None:
        await enroll(signer)
    return state.read_state()["gateway"][signer.address]


async def estimate(
    bearer: str, model: str, messages: list[dict[str, str]], max_tokens: int
) -> dict[str, Any]:
    response = await _request(
        "POST",
        "/models/estimate",
        bearer=bearer,
        json={"model": model, "messages": messages, "max_tokens": max_tokens},
    )
    if response.status_code != 200:
        _raise(response)
    return _body(response)


async def chat(
    bearer: str,
    model: str,
    messages: list[dict[str, str]],
    *,
    max_tokens: int,
    max_cost_flop: float,
    sleep=asyncio.sleep,
) -> GatewayReply:
    request_id = str(uuid.uuid4())
    for attempt in range(1, OVERLOAD_ATTEMPTS + 1):
        response = await _request(
            "POST",
            "/chat/completions",
            bearer=bearer,
            json={
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "max_cost_flop": max_cost_flop,
            },
            headers={"Idempotency-Key": request_id},
        )
        if response.status_code == 200:
            body = _body(response)
            usage = body.get("usage") or {}
            return GatewayReply(
                text=body["choices"][0]["message"].get("content") or "",
                model=model,
                request_id=request_id,
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
                cost_flop=usage.get("cost_flop"),
                receipt=body.get("x_flop_receipt"),
            )
        # A full channel pool clears within a minute or two; anything else is final.
        if response.status_code != 429 or attempt == OVERLOAD_ATTEMPTS:
            _raise(response)
        request_id = str(uuid.uuid4())
        await sleep(OVERLOAD_WAIT_SECONDS)
    raise AssertionError("unreachable")
