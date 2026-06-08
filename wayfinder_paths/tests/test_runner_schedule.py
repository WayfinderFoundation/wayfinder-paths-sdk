from __future__ import annotations

from wayfinder_paths.runner.schedule import (
    SCHEDULE_KIND_CRON,
    SCHEDULE_KIND_INTERVAL,
    next_run_after,
    normalize_schedule,
)


def test_interval_schedule_next_run() -> None:
    spec = normalize_schedule(interval_seconds=60)

    assert spec.kind == SCHEDULE_KIND_INTERVAL
    assert next_run_after(spec, now=1_000) == 1_060


def test_cron_schedule_next_run_utc() -> None:
    spec = normalize_schedule(cron_expr="*/15 * * * *")

    assert spec.kind == SCHEDULE_KIND_CRON
    assert spec.timezone == "UTC"
    assert next_run_after(spec, now=1_704_067_200) == 1_704_068_100


def test_cron_schedule_uses_timezone() -> None:
    spec = normalize_schedule(
        cron_expr="0 9 * * 1-5",
        timezone="America/Toronto",
    )

    next_run = next_run_after(spec, now=1_704_145_000)

    assert next_run == 1_704_204_000


def test_schedule_rejects_both_interval_and_cron() -> None:
    try:
        normalize_schedule(interval_seconds=60, cron_expr="* * * * *")
    except ValueError as exc:
        assert "exactly one" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_schedule_rejects_neither_interval_nor_cron() -> None:
    try:
        normalize_schedule()
    except ValueError as exc:
        assert "exactly one" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_schedule_rejects_non_positive_interval() -> None:
    try:
        normalize_schedule(interval_seconds=0)
    except ValueError as exc:
        assert "interval_seconds" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_schedule_rejects_seconds_style_cron() -> None:
    try:
        normalize_schedule(cron_expr="0 */5 * * * *")
    except ValueError as exc:
        assert "5-field" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_schedule_rejects_invalid_timezone() -> None:
    try:
        normalize_schedule(cron_expr="* * * * *", timezone="Not/AZone")
    except ValueError as exc:
        assert "invalid timezone" in str(exc)
    else:
        raise AssertionError("expected ValueError")
