from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_DEFAULT_SKIP_VALUES = {"", "_", "none", "null"}


class MCPArgumentError(ValueError):
    """Validation error for MCP arguments that should be returned to agents."""

    def __init__(
        self,
        message: str,
        *,
        field: str,
        received: Any = None,
        allowed_values: Iterable[str] | None = None,
        suggested_arguments: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.details: dict[str, Any] = {
            "field": field,
            "received": received,
            "received_type": type(received).__name__,
        }
        if allowed_values is not None:
            self.details["allowed_values"] = sorted(str(v) for v in allowed_values)
        if suggested_arguments:
            self.details["suggested_arguments"] = suggested_arguments


def optional_str(
    value: Any,
    *,
    field_name: str | None = None,
    skip_values: set[str] | None = None,
    max_length: int | None = 1000,
) -> str | None:
    skip = skip_values or _DEFAULT_SKIP_VALUES
    raw = str(value).strip()
    if raw.lower() in skip:
        return None
    if max_length is not None and len(raw) > max_length:
        raise MCPArgumentError(
            f"{field_name or 'value'} must be {max_length} characters or fewer",
            field=field_name or "value",
            received=value,
        )
    return raw


def normalize_int(
    value: Any,
    *,
    field_name: str,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int:
    if isinstance(value, bool):
        raise MCPArgumentError(
            f"{field_name} must be an integer",
            field=field_name,
            received=value,
        )
    try:
        if isinstance(value, float):
            if not value.is_integer():
                raise ValueError
            parsed = int(value)
        else:
            parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise MCPArgumentError(
            f"{field_name} must be an integer",
            field=field_name,
            received=value,
        ) from exc
    if min_value is not None and parsed < min_value:
        raise MCPArgumentError(
            f"{field_name} must be >= {min_value}",
            field=field_name,
            received=value,
        )
    if max_value is not None and parsed > max_value:
        raise MCPArgumentError(
            f"{field_name} must be <= {max_value}",
            field=field_name,
            received=value,
        )
    return parsed


def optional_int(
    value: Any,
    *,
    field_name: str,
    skip_values: set[str] | None = None,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int | None:
    if optional_str(value, skip_values=skip_values, max_length=None) is None:
        return None
    return normalize_int(
        value,
        field_name=field_name,
        min_value=min_value,
        max_value=max_value,
    )


def optional_iso8601(value: Any, *, field_name: str) -> str | None:
    normalized = optional_str(value, field_name=field_name)
    if normalized is None:
        return None
    try:
        datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MCPArgumentError(
            f"{field_name} must be an ISO-8601 date or timestamp",
            field=field_name,
            received=value,
            suggested_arguments={field_name: "2026-01-31T00:00:00Z"},
        ) from exc
    return normalized


def optional_iana_timezone(value: Any, *, field_name: str = "timezone") -> str | None:
    normalized = optional_str(value, field_name=field_name)
    if normalized is None:
        return None
    try:
        ZoneInfo(normalized)
    except ZoneInfoNotFoundError as exc:
        raise MCPArgumentError(
            f"{field_name} must be an IANA timezone such as UTC or America/Toronto",
            field=field_name,
            received=value,
            suggested_arguments={field_name: "UTC"},
        ) from exc
    return normalized


def optional_json_object(
    value: Any,
    *,
    field_name: str,
    skip_values: set[str] | None = None,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    raw = optional_str(value, skip_values=skip_values, max_length=None)
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise MCPArgumentError(
            f"{field_name} must be a JSON object",
            field=field_name,
            received=value,
            suggested_arguments={field_name: {}},
        ) from exc
    if not isinstance(parsed, dict):
        raise MCPArgumentError(
            f"{field_name} must be a JSON object",
            field=field_name,
            received=value,
            suggested_arguments={field_name: {}},
        )
    return parsed


def normalize_enum(
    value: Any,
    *,
    field_name: str,
    allowed_values: Iterable[str],
    aliases: dict[str, str] | None = None,
) -> str:
    allowed = {str(v).lower(): str(v) for v in allowed_values}
    normalized = str(value).strip().lower()
    if aliases and normalized in aliases:
        normalized = aliases[normalized].strip().lower()
    if normalized not in allowed:
        raise MCPArgumentError(
            f"{field_name} must be one of: {', '.join(sorted(allowed.values()))}",
            field=field_name,
            received=value,
            allowed_values=allowed.values(),
        )
    return allowed[normalized]


def normalize_human_amount(value: Any, *, field_name: str = "amount") -> str:
    """Validate the MCP convention for decimal human-unit token amounts."""
    if not isinstance(value, str):
        raise MCPArgumentError(
            f"{field_name} must be a decimal string such as '1.0', not "
            f"{type(value).__name__}",
            field=field_name,
            received=value,
            suggested_arguments={field_name: "1.0"},
        )
    normalized = value.strip()
    if "." not in normalized:
        raise MCPArgumentError(
            f"{field_name} must include a decimal point because it is in token "
            "units, not wei/base units",
            field=field_name,
            received=value,
            suggested_arguments={field_name: f"{normalized or '1'}.0"},
        )
    try:
        parsed = Decimal(normalized)
    except InvalidOperation as exc:
        raise MCPArgumentError(
            f"{field_name} must be a decimal string such as '1.25'",
            field=field_name,
            received=value,
            suggested_arguments={field_name: "1.0"},
        ) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise MCPArgumentError(
            f"{field_name} must be a positive finite decimal amount",
            field=field_name,
            received=value,
            suggested_arguments={field_name: "1.0"},
        )
    return normalized


def split_values(
    value: Any,
    *,
    field_name: str,
    max_items: int = 25,
    skip_values: set[str] | None = None,
) -> list[str] | None:
    skip = skip_values or _DEFAULT_SKIP_VALUES
    if isinstance(value, str):
        raw = optional_str(value, skip_values=skip, max_length=None)
        if raw is None:
            return None
        values = [
            item.strip() for item in raw.replace("\n", ",").split(",") if item.strip()
        ]
    elif isinstance(value, Iterable) and not isinstance(
        value, (bytes, bytearray, dict)
    ):
        values = [str(item).strip() for item in value if str(item).strip()]
    else:
        raise MCPArgumentError(
            f"{field_name} must be a string or list of strings",
            field=field_name,
            received=value,
        )
    if not values:
        return None
    if len(values) > max_items:
        raise MCPArgumentError(
            f"{field_name} must include {max_items} values or fewer",
            field=field_name,
            received=value,
        )
    return values
