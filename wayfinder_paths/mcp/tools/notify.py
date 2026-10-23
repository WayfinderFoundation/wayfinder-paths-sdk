from __future__ import annotations

import httpx
from typing_extensions import TypedDict

from wayfinder_paths.core.clients.NotifyClient import NOTIFY_CLIENT
from wayfinder_paths.mcp.utils import catch_errors, err, ok, throw_if_empty_str

TITLE_MAX = 200
MESSAGE_MAX = 20_000
SMS_MESSAGE_MAX = 500


class NotificationSettings(TypedDict, total=False):
    disabled: bool
    timezone: str
    quiet_hours_start: int
    quiet_hours_end: int
    initiative_level: int | None
    initiative_interval_minutes: int
    initiative_prompt: str


@catch_errors
async def notification_settings(updates: NotificationSettings | None = None) -> dict:
    """Read SMS preferences, or update only fields explicitly requested by the user.

    Omit updates to read (including while paused). Use disabled=true to pause all
    SMS, false to resume. Never edit plugins or configuration files to change SMS.
    timezone is an IANA name; quiet hours are local hours 0–23 (equal means none).
    initiative_level is the daily proactive budget, 1–12; 0/null disables check-ins.
    initiative_interval_minutes is 15–1440 in multiples of 15 (plan limits apply).
    initiative_prompt sets topics/interests. Omitted fields are unchanged.
    Trading permissions, phone numbers, bindings and models are app-only here.
    Never change preferences during an autonomous check-in or to bypass a pause.
    Return the saved settings to the user; do not claim success on an API error.
    """
    if (
        updates is not None
        and set(updates) - NotificationSettings.__annotations__.keys()
    ):
        return err("invalid_request", "Unsupported notification setting")
    try:
        return ok(await NOTIFY_CLIENT.settings(updates))
    except httpx.HTTPStatusError as exc:
        return _notify_http_error(exc)


def _notify_http_error(exc: httpx.HTTPStatusError) -> dict:
    try:
        body = exc.response.json()
    except ValueError:
        body = {"detail": exc.response.text}
    return err("notify_http_error", f"HTTP {exc.response.status_code}", body)


@catch_errors
async def notification_send(
    title: str, message: str, delivery: str = "email", override: bool = False
) -> dict:
    """Notify the OpenCode instance owner by email or by texting their phone.

    delivery="sms" sends `message` as a text to the user's phone — for
    finished, user-facing updates only, never progress notes. Plain text, hard
    cap 500 chars. Quiet hours and a daily budget gate unprompted texts: a
    blocked call returns a warning instead of sending, and only a repeat call
    with override=true pushes through — do that only for genuinely urgent
    information. A successful sms send reports the remaining daily budget and
    average spacing (daily_budget, remaining_budget,
    avg_seconds_between_messages) — pace unprompted texts across the day rather
    than spending the budget at once. Replies while the user is actively texting
    are never rate-limited, and near-duplicates of texts you already sent are
    rejected, so answering the user is always safe.
    A user-paused SMS binding cannot be overridden; use notification_settings
    only if the user explicitly asks to resume.

    delivery="email" (default) requires a verified email address and renders
    Markdown into a themed HTML email.

    Args:
        title: Short subject line (<= 200 chars).
        message: Body — Markdown for email, plain text (<= 500 chars) for sms.
        delivery: "email" (default) or "sms".
        override: sms only — set true ONLY on a re-call after this tool
            returned a quiet-hours or frequency warning, and only when the
            message is genuinely urgent.
    """
    if delivery not in ("email", "sms"):
        return err("invalid_request", 'delivery must be "email" or "sms"')
    title_s = throw_if_empty_str("title is required", title)
    if len(title_s) > TITLE_MAX:
        raise ValueError(f"title exceeds {TITLE_MAX} chars")
    throw_if_empty_str("message is required", message)

    limit = SMS_MESSAGE_MAX if delivery == "sms" else MESSAGE_MAX
    if len(message) > limit:
        raise ValueError(f"message exceeds {limit} chars")
    try:
        data = await NOTIFY_CLIENT.notify(
            title=title_s, message=message, delivery=delivery, override=override
        )
    except httpx.HTTPStatusError as exc:
        return _notify_http_error(exc)
    return ok(data)
