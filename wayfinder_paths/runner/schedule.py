from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

SCHEDULE_KIND_INTERVAL = "interval"
SCHEDULE_KIND_CRON = "cron"
DEFAULT_TIMEZONE = "UTC"


@dataclass(frozen=True)
class ScheduleSpec:
    kind: str
    interval_seconds: int | None = None
    cron_expr: str | None = None
    timezone: str = DEFAULT_TIMEZONE


def normalize_schedule(
    *,
    interval_seconds: int | None = None,
    cron_expr: str | None = None,
    timezone: str | None = None,
) -> ScheduleSpec:
    expr = str(cron_expr or "").strip()
    has_interval = interval_seconds is not None
    has_cron = bool(expr)
    if has_interval == has_cron:
        raise ValueError("provide exactly one of interval_seconds or cron_expr")

    if has_interval:
        interval = int(interval_seconds or 0)
        if interval <= 0:
            raise ValueError("interval_seconds must be > 0")
        return ScheduleSpec(kind=SCHEDULE_KIND_INTERVAL, interval_seconds=interval)

    tz_name = _normalize_timezone(timezone)
    if len(expr.split()) != 5:
        raise ValueError("cron_expr must use standard 5-field cron syntax")
    if not croniter.is_valid(expr):
        raise ValueError("cron_expr is invalid")
    return ScheduleSpec(
        kind=SCHEDULE_KIND_CRON,
        cron_expr=expr,
        timezone=tz_name,
    )


def schedule_from_job(job: dict[str, Any]) -> ScheduleSpec:
    kind = str(job.get("schedule_kind") or SCHEDULE_KIND_INTERVAL).strip().lower()
    if kind == SCHEDULE_KIND_CRON:
        return normalize_schedule(
            cron_expr=str(job.get("cron_expr") or ""),
            timezone=str(job.get("timezone") or DEFAULT_TIMEZONE),
        )
    return normalize_schedule(interval_seconds=int(job.get("interval_seconds") or 0))


def schedule_request_fields(spec: ScheduleSpec) -> dict[str, Any]:
    if spec.kind == SCHEDULE_KIND_INTERVAL:
        return {"interval_seconds": int(spec.interval_seconds or 0)}
    return {"cron_expr": spec.cron_expr, "timezone": spec.timezone}


def next_run_after(spec: ScheduleSpec, *, now: int) -> int:
    if spec.kind == SCHEDULE_KIND_INTERVAL:
        return int(now) + int(spec.interval_seconds or 0)

    if spec.kind != SCHEDULE_KIND_CRON or not spec.cron_expr:
        raise ValueError(f"unsupported schedule kind: {spec.kind}")

    tz = ZoneInfo(spec.timezone)
    base = datetime.fromtimestamp(int(now), tz=UTC).astimezone(tz)
    next_dt = croniter(spec.cron_expr, base).get_next(datetime)
    next_ts = int(next_dt.astimezone(UTC).timestamp())
    if next_ts <= int(now):
        raise ValueError("cron schedule did not produce a future run time")
    return next_ts


def _normalize_timezone(timezone: str | None) -> str:
    tz_name = str(timezone or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
    try:
        ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"invalid timezone: {tz_name}") from exc
    return tz_name
