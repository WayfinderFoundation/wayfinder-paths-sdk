from __future__ import annotations

import httpx

from wayfinder_paths.core.clients.NotifyClient import (
    NOTIFY_CLIENT,
    normalize_notify_delivery,
)
from wayfinder_paths.mcp.utils import catch_errors, err, ok, throw_if_empty_str

TITLE_MAX = 200
MESSAGE_MAX = 20_000


@catch_errors
async def shells_notify(title: str, message: str, delivery: str = "email") -> dict:
    """Notify the OpenCode instance owner by email or SMS.

    Email requires a verified email address and renders Markdown into a themed
    HTML email. SMS requires a verified phone number and sends plain text.

    Args:
        title: Short subject line (<= 200 chars).
        message: Markdown body (<= 20 000 chars).
        delivery: "email" (default), "sms", or "text".
    """
    title_s = throw_if_empty_str("title is required", title)
    if len(title_s) > TITLE_MAX:
        raise ValueError(f"title exceeds {TITLE_MAX} chars")
    throw_if_empty_str("message is required", message)
    if len(message) > MESSAGE_MAX:
        raise ValueError(f"message exceeds {MESSAGE_MAX} chars")
    try:
        delivery_s = normalize_notify_delivery(delivery)
    except ValueError as exc:
        return err("invalid_request", str(exc))

    try:
        data = await NOTIFY_CLIENT.notify(
            title=title_s,
            message=message,
            delivery=delivery_s,
        )
    except httpx.HTTPStatusError as exc:
        try:
            body = exc.response.json()
        except Exception:  # noqa: BLE001
            body = {"detail": exc.response.text}
        return err("notify_http_error", f"HTTP {exc.response.status_code}", body)
    return ok(data)
