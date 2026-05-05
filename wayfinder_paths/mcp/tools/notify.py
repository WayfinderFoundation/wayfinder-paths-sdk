from __future__ import annotations

import httpx

from wayfinder_paths.core.clients.NotifyClient import NOTIFY_CLIENT
from wayfinder_paths.mcp.utils import err, ok

TITLE_MAX = 200
MESSAGE_MAX = 20_000


async def shells_notify(title: str, message: str) -> dict:
    """Email the OpenCode instance owner (verified email only).

    The message is rendered from Markdown into a themed HTML email on
    vault-backend. Use headings, lists, code blocks, links, etc.

    Args:
        title: Short subject line (<= 200 chars).
        message: Markdown body (<= 20 000 chars).
    """
    title_s = (title or "").strip()
    if not title_s:
        return err("invalid_request", "title is required")
    if len(title_s) > TITLE_MAX:
        return err("invalid_request", f"title exceeds {TITLE_MAX} chars")
    if not message:
        return err("invalid_request", "message is required")
    if len(message) > MESSAGE_MAX:
        return err("invalid_request", f"message exceeds {MESSAGE_MAX} chars")

    try:
        data = await NOTIFY_CLIENT.notify(title=title_s, message=message)
    except httpx.HTTPStatusError as exc:
        try:
            body = exc.response.json()
        except Exception:  # noqa: BLE001
            body = {"detail": exc.response.text}
        return err("notify_http_error", f"HTTP {exc.response.status_code}", body)
    except Exception as exc:  # noqa: BLE001
        return err("notify_error", str(exc))
    return ok(data)
