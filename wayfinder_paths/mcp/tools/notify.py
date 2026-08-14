from __future__ import annotations

import httpx

from wayfinder_paths.core.clients.NotifyClient import (
    NOTIFY_CLIENT,
    normalize_notify_delivery,
)
from wayfinder_paths.mcp.utils import catch_errors, err, ok, throw_if_empty_str

TITLE_MAX = 200
MESSAGE_MAX = 20_000
MOBILE_MESSAGE_MAX = 500


async def _relay(request) -> dict:
    try:
        data = await request
    except httpx.HTTPStatusError as exc:
        try:
            body = exc.response.json()
        except Exception:  # noqa: BLE001
            body = {"detail": exc.response.text}
        return err("notify_http_error", f"HTTP {exc.response.status_code}", body)
    return ok(data)


@catch_errors
async def notification_send(
    title: str, message: str, delivery: str = "email", override: bool = False
) -> dict:
    """Notify the OpenCode instance owner by email or by texting their phone.

    delivery="mobile" sends `message` as a text into the user's active mobile
    conversation (iMessage/SMS) — this actually lands on their phone, so it is
    for finished, user-facing updates only, never progress notes. Mobile texts
    are plain text, hard cap 500 chars. Quiet hours and a frequency budget
    gate unprompted mobile sends: a blocked call returns a warning instead of
    sending, and only a repeat call with override=true pushes through — do
    that only for genuinely urgent information. Replies while the user is
    actively texting are never rate-limited, and near-duplicates of texts you
    already sent are rejected, so answering the user is always safe.

    delivery="email" (default) requires a verified email address and renders
    Markdown into a themed HTML email.

    Args:
        title: Short subject line (<= 200 chars).
        message: Body — Markdown for email, plain text (<= 500 chars) for
            mobile.
        delivery: "email" (default) or "mobile".
        override: Mobile only — set true ONLY on a re-call after this tool
            returned a quiet-hours or frequency warning, and only when the
            message is genuinely urgent.
    """
    title_s = throw_if_empty_str("title is required", title)
    if len(title_s) > TITLE_MAX:
        raise ValueError(f"title exceeds {TITLE_MAX} chars")
    throw_if_empty_str("message is required", message)
    try:
        delivery_s = normalize_notify_delivery(delivery)
    except ValueError as exc:
        return err("invalid_request", str(exc))

    limit = MOBILE_MESSAGE_MAX if delivery_s == "mobile" else MESSAGE_MAX
    if len(message) > limit:
        raise ValueError(f"message exceeds {limit} chars")
    return await _relay(
        NOTIFY_CLIENT.notify(
            title=title_s, message=message, delivery=delivery_s, override=override
        )
    )
